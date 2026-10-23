"""Venue protocol and the Stellar FIX connection adapter.

Stellar speaks FIX 4.4 over two sessions, both with TargetCompID STELLAR:
the Order Router (orders and execution reports — Stellar FIX Order Router
Specification, 03/04/2018) and the Market Data feed (Security List, Market
Data Request / Snapshot / Incremental Refresh, Security Status — Stellar FIX
Market Data Feed Specification, 29/09/2017). Instrument, quote and reviewed
manual order workflows live in ManualTerminal; the Algo's orders in
AlgoOrderRouter, below. The strategy protocol reports unknown account
positions/orders as None.
"""

import copy
import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from .models import (BookTop, Fill, GatewayEvent, OrderRequest, OrderState,
                     SecurityDef, SessionState, VenueOrder, VenuePosition)
from .manual_terminal import ManualTerminal
from .fix_audit import FixAuditLog
from .algo_feed import AlgoDataFeed, MarketDataEvent

logger = logging.getLogger(__name__)

#: Every order we send carries this. Anything at the venue without it belongs
#: to somebody else — a hand order in Stellar, most likely — and is never
#: cancelled, amended or counted as ours.
CLORDID_PREFIX = "FT"

#: The FIX version both Stellar sessions speak.
BEGIN_STRING = "FIX.4.4"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


#: The name the FIX log gives the Order Status sweep after each logon.
RECOVERY_SESSION = 'Order Router'
#: How long Stellar has to answer every Order Status Request of the sweep
#: before it is reported FAILED.
RECOVERY_TIMEOUT_SEC = 60.0
#: How long a client-initiated Logout waits for Stellar's own Logout before
#: the socket is closed anyway (Stellar's conformance test: "Client initiates
#: Logout" — a clean logout is one Stellar answered).
LOGOUT_WAIT_SEC = 3.0


@runtime_checkable
class Gateway(Protocol):
    """What the rest of the system is allowed to ask a venue for.

    Note the return types that include None. `orders()` and `positions()`
    return None for **"could not read"**, which is not "there are none".
    """

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def state(self) -> SessionState: ...
    def state_text(self) -> str: ...
    def subscribe(self, contract) -> None: ...
    def top_of_book(self, key: str) -> Optional[BookTop]: ...
    def security_definition(self, contract) -> Optional[SecurityDef]: ...
    def margin_for(self, key: str, qty: float) -> Optional[float]: ...
    def send(self, order: OrderRequest) -> str: ...
    def cancel(self, clordid: str) -> None: ...
    def amend(self, clordid: str, price: Optional[float] = None,
              qty: Optional[float] = None) -> None: ...
    def orders(self) -> Optional[List[VenueOrder]]: ...
    def positions(self) -> Optional[List[VenuePosition]]: ...
    def drain_events(self) -> List[GatewayEvent]: ...


class SequenceStore:
    """The Order Router's sequence numbers, kept across restarts.

    Stellar honours a ResendRequest on the Order Router, so a session that
    comes back on the numbers it left at is SENT what it missed while it was
    down — the execution reports of fills that happened meanwhile. A session
    that reset to 1 on every logon would lose them. The outgoing number is
    written BEFORE each message goes (a number used twice is a broken
    session; one skipped is a gap Stellar asks us to fill, which we do with a
    GapFill); the incoming one at most once a second and at the end — read
    back low, it only means Stellar resends what was already applied, which
    is never applied twice.
    """

    def __init__(self, path: str = ':memory:'):
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute('CREATE TABLE IF NOT EXISTS fix_sequences '
                        '(session TEXT PRIMARY KEY, data TEXT)')
        self.db.commit()

    def load(self, key: str) -> Dict[str, Any]:
        with self.lock:
            row = self.db.execute('SELECT data FROM fix_sequences WHERE session=?',
                                  (key,)).fetchone()
        try:
            return json.loads(row[0]) if row else {}
        except (TypeError, ValueError):
            return {}

    def save(self, key: str, **values) -> None:
        with self.lock:
            row = self.db.execute('SELECT data FROM fix_sequences WHERE session=?',
                                  (key,)).fetchone()
            data = json.loads(row[0]) if row else {}
            data.update(values, saved=utcnow().isoformat())
            self.db.execute('INSERT OR REPLACE INTO fix_sequences VALUES (?,?)',
                            (key, json.dumps(data)))
            self.db.commit()

    def reset(self, key: str) -> None:
        self.save(key, out_seq=0, in_seq=0)


class FixGateway:
    """Stellar connectivity: the Order Router and Market Data sessions.

    Manual execution is isolated from the strategy gateway, which does not
    reconcile or automatically execute against the operator's manual book.
    """
    #: The Algo's orders go to Stellar over the Order Router
    #: (`AlgoOrderRouter`). Whether they are SENT is the engine's choice:
    #: PAPER after every restart, LIVE only when armed and confirmed.
    connection_only = False

    def __init__(self, venue, contracts=None, manual_path=':memory:'):
        self.venue = venue
        self.contracts = list(contracts or [])
        self._sessions = {}
        self._text = "Disconnected"
        self._lock = threading.RLock()
        self._activity = deque(maxlen=80)
        self._activity_lock = threading.Lock()
        self._reconnect_at = None
        self._connect_not_before = 0
        # Disk IO never runs on the latency-sensitive FIX receiver threads.
        self.audit = FixAuditLog(async_write=True)
        self._contract_security_ids = {}
        self.algo_feed = AlgoDataFeed()
        self.sequences = SequenceStore(manual_path)
        self.terminal = ManualTerminal(self, manual_path)
        self.algo = AlgoOrderRouter(self)
        #: The Order Status sweep after each Order Router logon: Stellar is
        #: asked for the state of every order of ours still recorded as
        #: working (Stellar has no recovery service of its own).
        self._sweep = None
        self.recovery = {'state': 'NOT_RUN', 'text': 'Not run yet in this session.',
                         'mode': None, 'replayed': 0, 'started': None,
                         'finished': None, 'note': None}

    def start(self):
        with self._lock:
            if time.monotonic() < self._connect_not_before:
                self._reconnect_at = self._connect_not_before
                self._text = 'Waiting before reconnecting to Stellar'
                return
            if self._sessions and all(s.is_running() for s in self._sessions.values()):
                return
            v = self.venue
            if v.environment != 'UAT':
                # As on the TT desk this was ported from: production stays
                # refused until this adapter has been proven on Stellar UAT.
                self._text = "This Stellar adapter runs on a UAT venue only"
                return
            if v.fix_version != BEGIN_STRING:
                self._text = f"Stellar speaks {BEGIN_STRING}; this venue is set to {v.fix_version}"
                return
            common = dict(target_comp_id=v.target_comp_id or 'STELLAR',
                          sender_sub_id=v.sender_sub_id,
                          target_sub_id=v.target_sub_id,
                          on_behalf_of_comp_id=v.on_behalf_of_comp_id,
                          on_behalf_of_sub_id=v.on_behalf_of_sub_id,
                          heartbeat=v.heartbeat_sec, use_tls=v.use_tls,
                          username=v.username)
            # The Order Router keeps its sequence numbers (Stellar resends
            # what was missed); Market Data resets on every logon (Stellar
            # does not support a resend there — a fresh snapshot is asked for
            # instead).
            configs = {'Order Routing': dict(common, host=v.host, port=v.port,
                sender_comp_id=v.sender_comp_id, password=v.password or '',
                reset_on_logon=bool(v.reset_seq_on_logon), persist=True)}
            if v.md_host:
                configs['Market Data'] = dict(common, host=v.md_host, port=v.md_port,
                    sender_comp_id=v.md_sender_comp_id,
                    target_comp_id=v.md_target_comp_id or v.target_comp_id or 'STELLAR',
                    password=v.md_password or '',
                    reset_on_logon=True, persist=False)
            for name, cfg in configs.items():
                # No password is allowed: Stellar's Logon (98 / 108) carries
                # none, and a session may be authorised by its address alone.
                missing = [k for k in ('host', 'port', 'sender_comp_id', 'target_comp_id')
                           if not cfg.get(k)]
                if missing:
                    self._text = name + ': missing ' + ', '.join(missing)
                    return
            for name, cfg in configs.items():
                cfg['allowed_messages'] = (('x', 'V') if name == 'Market Data'
                                           else ('D', 'AB', 'F', 'G', 'AC', 'H', 'AF'))
                existing = self._sessions.get(name)
                if existing is not None:
                    if existing.is_running():
                        continue
                    existing.stop()
                    existing.thread.join(timeout=17)
                state = SimpleNamespace(lock=threading.RLock(), status='DISCONNECTED',
                    error='', out_seq=0, in_seq=0, incoming_count=0, outgoing_count=0,
                    last_message='', last_heartbeat='', last_logon='')
                session = NativeFixSession(self, state, name, cfg)
                self._sessions[name] = session
                session.start()

    def stop(self):
        with self._lock:
            self._reconnect_at = None
            for session in self._sessions.values():
                session.stop()
            for session in self._sessions.values():
                session.thread.join(timeout=17)
            self._sessions.clear()
            self.algo_feed.clear()
            self._text = 'Disconnected'
            self._connect_not_before = time.monotonic() + 10

    def reconnect(self):
        self.stop()
        self._reconnect_at = time.monotonic() + 10
        self._text = 'Reconnecting in 10 seconds'

    def reset_sequences(self) -> Dict[str, Any]:
        """Start the Order Router's numbers again from 1 on the next logon —
        ONLY when Stellar says it has reset its side (a daily or weekly
        reset). Refused while the session is up."""
        with self._lock:
            s = self._sessions.get('Order Routing')
            if s is not None and s.is_running():
                return {'ok': False, 'error': 'Disconnect the Order Router first.'}
            self.sequences.reset(self._sequence_key())
            self.audit.write(level='WARNING', category='FIX Session',
                             session='Order Routing', direction='LOCAL',
                             event='Sequence numbers reset', sequence='',
                             details={'next_out': 1, 'next_in': 1})
            return {'ok': True}

    def skip_sequence(self, by: int = 10) -> Dict[str, Any]:
        """Raise the Order Router's NEXT outgoing number by `by` — for
        Stellar's conformance test of a GapFill only: "Client disconnects.
        Client increases outgoing message sequence number. Client logs in.
        FIX gateway submits resend request. Client sends a gap fill."
        Stellar's ResendRequest for the skipped numbers is answered with a
        SequenceReset-GapFill, as every ResendRequest is: nothing is lost,
        nothing resent. Refused while the session is up."""
        by = int(by)
        if not 1 <= by <= 1000:
            return {'ok': False, 'error': 'Skip between 1 and 1000 numbers.'}
        with self._lock:
            s = self._sessions.get('Order Routing')
            if s is not None and s.is_running():
                return {'ok': False, 'error': 'Disconnect the Order Router first.'}
            key = self._sequence_key()
            saved = self.sequences.load(key)
            before = int(saved.get('out_seq') or 0)
            self.sequences.save(key, out_seq=before + by)
            self.audit.write(level='WARNING', category='FIX Session',
                             session='Order Routing', direction='LOCAL',
                             event='Outgoing sequence skipped ahead', sequence='',
                             details={'next_out_was': before + 1, 'next_out': before + by + 1})
            return {'ok': True, 'next_out': before + by + 1,
                    'text': f'The next Order Router logon goes as {before + by + 1} '
                            f'(was {before + 1}): Stellar should ask for the gap, and '
                            f'it is answered with a GapFill.'}

    def _sequence_key(self) -> str:
        v = self.venue
        return f"{v.sender_comp_id}->{v.target_comp_id or 'STELLAR'}@{v.host}:{v.port}"

    # -- the Order Status sweep (what replaces TT's FIX Recovery) -------------

    def order_session_logon(self) -> str:
        """When the Order Router last logged on ('' if it is not up): a new
        value is a new logon, after which our working orders are asked for."""
        s = self._sessions.get('Order Routing')
        if s is None or not self.order_session_ready():
            return ''
        return str(getattr(s.state, 'last_logon', '') or '')

    def recover_missed(self, mode: str = 'auto',
                       now: Optional[datetime] = None) -> Dict[str, Any]:
        """Ask Stellar for the state of every order of ours still recorded as
        working — the Algo's (adopted from the last run or sent this one)
        and the manual tickets — with an Order Status Request (H) each,
        by its OrderID. An order of ours whose OrderID was never heard (its
        acknowledgement lost) cannot be asked about by H, nor cancelled:
        while there is one, ONE Mass Order Status Request (AF, 585=7 — all
        orders) goes too, and Stellar's answer names it by its ClOrdID with
        its OrderID. That answer ends at the report marked 912=Y.

        What Stellar sent while the line was down comes back by itself: the
        Order Router keeps its sequence numbers, so Stellar is asked to
        resend the gap at the logon (`NativeFixSession`). The sweep is the
        second line: an order whose reports were lost beyond that is put
        right (cancelled, filled — said if its fills are not on the book)."""
        now = now or utcnow()
        with self._lock:
            if self.recovery.get('state') == 'RUNNING':
                return {'ok': False, 'error': 'An order status sweep is already running.'}
            session = self._sessions.get('Order Routing')
            if session is None or session.state.status != 'CONNECTED':
                return {'ok': False, 'error': 'The Order Router is not connected.'}
            requests = self.algo.status_targets() + self.terminal.status_targets()
            unheard = self.algo.unheard_orders() + self.terminal.unheard_orders()
            self.recovery = {'state': 'RUNNING', 'mode': 'ORDER_STATUS',
                             'text': f'Asking Stellar for the status of '
                                     f'{len(requests) + len(unheard)} order(s) of ours '
                                     f'recorded as working.',
                             'replayed': 0, 'started': now.isoformat(),
                             'finished': None, 'note': None}
            self._sweep = {'waiting': {}, 'started': time.monotonic(), 'unanswered': []}
            for target in requests:
                req = 'OS-' + uuid.uuid4().hex[:12]
                fields = [('11', target['clordid']), ('37', target['order_id']),
                          ('790', req)] + target.get('fields', [])
                try:
                    session.send('H', fields + [('60', fix_timestamp())])
                    self._sweep['waiting'][req] = target
                except Exception as e:                       # noqa: BLE001
                    self._sweep['unanswered'].append(
                        f"{target['clordid']}: not asked ({self._redact(str(e))})")
            if unheard:
                req = 'MS-' + uuid.uuid4().hex[:12]
                names = ', '.join(unheard)
                try:
                    session.send('AF', [('584', req), ('585', '7'), ('60', fix_timestamp())])
                    self._sweep['waiting'][req] = {
                        'clordid': f'{names} (no OrderID heard; asked by Mass Order Status)'}
                except Exception as e:                       # noqa: BLE001
                    self._sweep['unanswered'].append(
                        f"{names}: not asked ({self._redact(str(e))})")
            self._sweep_check()
            return {'ok': True, 'text': self.recovery['text']}

    def status_answered(self, req_id: str) -> None:
        """An Execution Report carrying one of the sweep's OrdStatusReqIDs."""
        with self._lock:
            if self._sweep and self._sweep['waiting'].pop(req_id, None) is not None:
                self.recovery['replayed'] = self.recovery.get('replayed', 0) + 1
                self._sweep_check()

    def _sweep_check(self) -> None:
        sweep = self._sweep
        if sweep is None or self.recovery.get('state') != 'RUNNING':
            return
        if not sweep['waiting'] and not sweep['unanswered']:
            self._recovery_done('DONE', '', self.recovery.get('replayed', 0))
        elif not sweep['waiting']:
            self._recovery_done('FAILED', '; '.join(sweep['unanswered']),
                                self.recovery.get('replayed', 0))
        elif time.monotonic() - sweep['started'] > RECOVERY_TIMEOUT_SEC:
            names = ', '.join(t['clordid'] for t in sweep['waiting'].values())
            self._recovery_done('FAILED', f'Stellar did not answer within '
                                f'{RECOVERY_TIMEOUT_SEC:.0f} seconds for {names}',
                                self.recovery.get('replayed', 0))

    def _recovery_done(self, outcome: str, text: str, replayed: int) -> None:
        words = {'DONE': f'Stellar answered for {replayed} order(s); each applied '
                         f'once — nothing already booked was booked again.',
                 'REFUSED': f'Stellar refused the order status request: {text}',
                 'FAILED': f'The order status sweep did not finish: {text}'}
        self.recovery.update(state=outcome, text=words[outcome], replayed=replayed,
                             finished=utcnow().isoformat(), venue_text=text)
        self._sweep = None
        self.audit.write(level='INFO' if outcome == 'DONE' else 'ERROR',
                         category='FIX Session', session=RECOVERY_SESSION,
                         direction='LOCAL', event='Order status sweep ' + outcome.lower(),
                         sequence='', details={'answered': replayed,
                                               'text': self._redact(text)})

    def recovery_status(self) -> Dict[str, Any]:
        with self._lock:
            self._sweep_check()
            out = dict(self.recovery)
        s = self._sessions.get('Order Routing')
        last = getattr(getattr(s, 'state', None), 'last_message', '') if s else ''
        out['last_heard'] = last or None
        return out

    def recovery_running(self) -> bool:
        """Read on every engine pass: no database here."""
        with self._lock:
            self._sweep_check()
            return self.recovery.get('state') == 'RUNNING'

    def order_session_ready(self) -> bool:
        """The Order Router is logged on and what it missed has been resent."""
        s = self._sessions.get('Order Routing')
        return bool(s is not None and getattr(s, 'ready', lambda: True)())

    def state(self):
        if self._reconnect_at is not None:
            return SessionState.CONNECTING
        states = [s.state.status for s in self._sessions.values()]
        if states and all(s == 'CONNECTED' for s in states):
            return SessionState.LOGGED_ON
        if 'ERROR' in states:
            return SessionState.ERROR
        if 'CONNECTING' in states:
            return SessionState.CONNECTING
        return SessionState.DOWN

    def state_text(self):
        if not self._sessions:
            return self._text
        return '; '.join(name + ': ' + s.state.status +
            (' ? ' + self._redact(s.state.error) if s.state.error else '')
            for name, s in self._sessions.items())

    def _redact(self, text):
        for session in self._sessions.values():
            password = session.cfg.get('password')
            if password:
                text = text.replace(password, '[redacted]')
        return text

    def log_fix(self, session, direction, msg_type, seq_num, raw):
        # Raw protocol evidence is durable; sensitive fields are redacted by the writer.
        names = {'A': 'Logon', '0': 'Heartbeat', '1': 'Test request',
                 '5': 'Logout', '2': 'Resend request', '3': 'Reject', '4': 'Sequence reset'}
        fields = parse_fix_message(raw)
        category = ('Market Data' if msg_type in ('x', 'y', 'f', 'V', 'W', 'X', 'Y')
                    else 'Executions' if msg_type in ('8', '9')
                    else 'Orders' if msg_type in ('D', 'F', 'G', 'AB', 'AC', 'H', 'AF')
                    else 'FIX Session')
        details = {key: fields.get(tag) for key, tag in {
            'instrument': '55', 'security_id': '48', 'client_order_id': '11',
            'order_id': '37', 'execution_id': '17', 'execution_type': '150',
            'order_status': '39', 'side': '54', 'quantity': '38', 'price': '44',
            'fill_quantity': '32', 'fill_price': '31', 'remaining_quantity': '151',
            'average_price': '6', 'reason': '58', 'fix_sending_time': '52'
        }.items() if fields.get(tag) not in (None, '')}
        # A client-requested Logout is normal lifecycle activity, not an
        # error. An inbound Logout deserves attention, while explicit FIX,
        # order-change and market-data rejects remain errors.
        level = ('ERROR' if msg_type in ('3', '9', 'Y', 'j') else
                 'WARNING' if msg_type == '5' and direction == 'IN' else 'INFO')
        self.audit.write(level=level,
                         category=category, session=session, direction=direction,
                         event=names.get(msg_type, msg_type), sequence=str(seq_num),
                         details=details, raw=raw)
        with self._activity_lock:
            self._activity.append({'time': utcnow().isoformat(), 'session': session,
                'direction': direction, 'type': names.get(msg_type, msg_type),
                'sequence': seq_num})

    def connection_snapshot(self):
        sessions = []
        for name in ('Order Routing', 'Market Data'):
            session = self._sessions.get(name)
            md = name == 'Market Data'
            v = self.venue
            row = {'name': name, 'host': v.md_host if md else v.host,
                   'port': v.md_port if md else v.port,
                   'sender_comp_id': v.md_sender_comp_id if md else v.sender_comp_id,
                   'target_comp_id': ((v.md_target_comp_id or v.target_comp_id) if md
                                      else v.target_comp_id) or 'STELLAR',
                   'password_set': v.has_md_password if md else v.has_password,
                   'status': 'CONNECTING' if self._reconnect_at else 'DISCONNECTED',
                   'error': '', 'incoming_count': 0, 'outgoing_count': 0,
                   'in_seq': 0, 'out_seq': 0, 'last_message': '',
                   'last_heartbeat': '', 'last_logon': ''}
            if session:
                with session.state.lock:
                    for key in ('status', 'error', 'incoming_count', 'outgoing_count',
                                'in_seq', 'out_seq', 'last_message', 'last_heartbeat', 'last_logon'):
                        row[key] = getattr(session.state, key, '')
                row['error'] = self._redact(row['error'])
            sessions.append(row)
        with self._activity_lock:
            activity = list(reversed(self._activity))
        return {'venue': self.venue.name, 'environment': self.venue.environment,
                'recovery': self.recovery_status(),
                'state': self.state().value, 'text': self.state_text(),
                'sessions': sessions, 'activity': activity,
                'reconnect_in': max(0, round(self._reconnect_at - time.monotonic())) if self._reconnect_at else 0}

    def subscribe(self, contract):
        """Register a configured contract with the Market Data parser.

        The FIX session is asynchronous, so registration is deliberately
        separate from sending V.  ``ManualTerminal.poll`` sends the request
        as soon as Market Data is logged on (and re-sends it after reconnect),
        and asks Stellar for the contract's Security List entry — its CFI
        code, maturity, legs, tick and tick value, which an ORDER needs —
        when it has not been seen yet.
        """
        security_id = str(getattr(contract, 'security_id', '') or '').strip()
        symbol = str(getattr(contract, 'symbol', '') or '').strip()
        exchange = str(getattr(contract, 'security_exchange', '') or '').strip()
        if not security_id or not symbol:
            return None
        with self.terminal.lock:
            # Stellar's own definition WINS. The stub only fills what Stellar
            # has not said; the contract's configured tick size stands in
            # until Stellar's Security List entry is seen.
            known = (self.terminal.watch.get(security_id)
                     or self.terminal.catalogue.get(security_id) or {})
            instrument = copy.deepcopy(known)
            for field, value in (
                    ('security_id', security_id), ('symbol', symbol),
                    ('exchange', exchange),
                    ('description', getattr(contract, 'name', '') or symbol),
                    ('security_type', ''), ('maturity', ''), ('legs', []),
                    ('parameters', {}), ('full_depth', False)):
                if instrument.get(field) in (None, ''):
                    instrument[field] = value
            tick = getattr(contract, 'tick_size', None)
            if not instrument.get('tick_size') and tick:
                instrument['tick_size'] = format(tick, 'g')
            self.terminal._enrich(instrument)
            if not instrument.get('tick_value') and getattr(contract, 'tick_value', None):
                instrument['tick_value'] = format(contract.tick_value, 'g')
            self._contract_security_ids[contract.key] = security_id
            self.terminal.watch[security_id] = instrument
            self.terminal.catalogue[security_id] = copy.deepcopy(instrument)
            self.terminal._save('watch', security_id, instrument)
            session = self._sessions.get('Market Data')
            if (session is not None and session.state.status == 'CONNECTED'
                    and session.is_running()
                    and getattr(session, 'ready', lambda: True)()):
                self.terminal._subscribe(security_id)
        return None

    #: How many levels a side the desk ladder is given — the rows it can
    #: draw, not the whole book: the snapshot is written twice a second.
    DEPTH_LEVELS = 30

    def depth(self, key):
        """The book for the desk ladder: every level Stellar publishes, each
        side best first, and whether FULL depth (264=0) was asked for or only
        the top (264=1). None while Market Data is down or the contract has
        no Security ID — unknown, never an empty book."""
        md = self._sessions.get('Market Data')
        if md is None or md.state.status != 'CONNECTED' or not md.is_running():
            return None
        security_id = self._contract_security_ids.get(key)
        if not security_id:
            return None
        with self.terminal.lock:
            book = self.terminal.books.get(security_id) or {}
            watch = self.terminal.watch.get(security_id) or {}
            return {'full': bool(watch.get('full_depth')),
                    'bids': [dict(x) for x in (book.get('bid_levels') or [])[:self.DEPTH_LEVELS]],
                    'asks': [dict(x) for x in (book.get('ask_levels') or [])[:self.DEPTH_LEVELS]]}

    def top_of_book(self, key):
        md = self._sessions.get('Market Data')
        if md is None or md.state.status != 'CONNECTED' or not md.is_running():
            return None
        security_id = self._contract_security_ids.get(key, key)
        with self.terminal.lock:
            if self.terminal.price_factor(security_id) is None:
                return None
            book = self.terminal.books.get(security_id)
            # One side alone IS returned: it is what Stellar is publishing,
            # and the window has to show it rather than a blank that looks
            # like no subscription. It is not a usable book — BookTop.usable
            # is False, so it gives no mid, no statistic and no entry.
            if (not book or book.get('integrity_ok') is False
                    or (book.get('bid') is None and book.get('ask') is None)):
                return None
            stamp = book.get('timestamp')
            try:
                ts = datetime.fromisoformat(stamp) if stamp else None
            except (TypeError, ValueError):
                ts = None
            return BookTop(bid=book.get('bid'), ask=book.get('ask'),
                           bid_size=book.get('bid_size'), ask_size=book.get('ask_size'),
                           ts=ts)

    def on_market_book(self, security_id, book):
        """Called after the FIX parser updates a book, under its lock."""
        for key, mapped_id in self._contract_security_ids.items():
            if mapped_id != security_id:
                continue
            stamp = book.get('timestamp')
            try:
                received_at = datetime.fromisoformat(stamp) if stamp else utcnow()
            except (TypeError, ValueError):
                received_at = utcnow()
            event = MarketDataEvent(
                contract_key=key, security_id=security_id,
                symbol=self.terminal.watch.get(security_id, {}).get('symbol', ''),
                bid=book.get('bid'), ask=book.get('ask'), last=book.get('last'),
                bid_size=book.get('bid_size'), ask_size=book.get('ask_size'),
                received_at=received_at, sequence=str(book.get('fix_sequence') or ''))
            if self.algo_feed.publish(event):
                logger.debug('normalized FIX quote %s seq=%s bid=%s ask=%s last=%s',
                             key, event.sequence, event.bid, event.ask, event.last)

    def drain_market_data(self):
        return self.algo_feed.drain()

    def security_definition(self, contract):
        return None

    def margin_for(self, key, qty):
        return None

    def send(self, order):
        return self.algo.send(order)

    def cancel(self, clordid):
        self.algo.cancel(clordid)

    def amend(self, clordid, price=None, qty=None):
        self.algo.amend(clordid, price=price, qty=qty)

    def adopt(self, row):
        return self.algo.adopt(row)

    def orders(self):
        # Unknown, which is NOT "none". Our own orders are swept by their
        # recorded ids (H), and by Mass Order Status (AF) only for one whose
        # OrderID was never heard: an AF answer lists every order on the
        # login, and the desk touches nothing it did not send.
        return None

    def positions(self):
        return self.algo.positions()

    def positions_status(self):
        return self.algo.positions_status()

    def drain_events(self):
        if self._reconnect_at is not None and time.monotonic() >= self._reconnect_at:
            self._reconnect_at = None
            self.start()
        self.terminal.poll()
        self.algo.request_positions(time.monotonic())
        self.algo.flush_deferred()
        return self.algo.drain()

    def diagnose(self):
        state = self.state()
        # CONNECTING is a handshake in progress, not a failure: the Logon is
        # on the wire and Stellar has up to 20 s to answer it.
        pending = state == SessionState.CONNECTING
        return [{'check': 'Stellar FIX sessions', 'ok': state == SessionState.LOGGED_ON,
                 'pending': pending,
                 'detail': self.state_text(),
                 'fix': ('Logon sent — waiting for Stellar to answer (up to 20 seconds). '
                         'This page keeps checking; nothing has failed yet.')
                        if pending else self._connection_fix()},
                {'check': 'Positions', 'ok': self.algo.positions_status()['status'] == 'complete',
                 'detail': (self.algo.positions_status()['why']
                            or self.algo.positions_status()['status']),
                 'fix': ('Stellar positions cannot be read on this session: arming the '
                         'Algo LIVE asks you to confirm that this book\'s own fills '
                         'are the record.')}]

    def _connection_fix(self):
        """Give the operator the remedy for the observed connection failure.

        A WinError 10013 is raised by Windows while opening the TCP socket.
        No FIX Logon has been put on the wire at that point, so suggesting a
        password change is both misleading and delays the actual repair.
        """
        errors = ' '.join(str(session.state.error) for session in self._sessions.values()).lower()
        if 'sequence mismatch' in errors or 'msgseqnum too low' in errors \
                or 'sequence number' in errors:
            return ('The FIX sequence numbers are out of step with Stellar. Do not reconnect or reset them '
                    'until Stellar confirms its side of the Order Router sequence/reset and all '
                    'working orders and fills have been reconciled. The gateway cannot '
                    'safely skip a missing order-session message. If Stellar has reset '
                    'its side, use "Reset sequence numbers" on the Exchanges page.')
        if 'delayed logon processing' in errors:
            return ('Stellar rejected the Order Router logon as delayed. Verify the host clock is '
                    'synchronized, then ask Stellar to confirm the session latency limit before '
                    'retrying the logon.')
        if 'winerror 10013' in errors or 'access permissions' in errors:
            return ('Windows is blocking outbound TCP before FIX Logon. Allow the Python '
                    'executable through the firewall/endpoint security for the Stellar '
                    'Order Router and Market Data ports, or use a network that '
                    'permits those ports. Credentials are not involved in this error.')
        if 'already connected' in errors or 'already logged' in errors:
            return ('Stellar already has these sessions logged on from somewhere else — '
                    'usually another copy of this program still running (an older '
                    'start.py window, or a second PC using the same .env). Close it, '
                    'then press Connect. If nothing else is running, the last run was '
                    'closed without logging out and Stellar still holds the session: wait '
                    'about a minute for Stellar to drop it, then Connect. The credentials '
                    'are fine — Stellar recognised them.')
        if 'did not answer the logon' in errors:
            return ('Stellar accepted the connection but never answered the Logon. A FIX '
                    'server usually stays silent when it does not recognise the session: '
                    'confirm the SenderCompID / TargetCompID (STELLAR) — and the password '
                    'in .env, if Stellar issued one — match what Stellar provisioned for '
                    'this session, and that the session is enabled and this address allowed.')
        if 'timed out' in errors:
            return ('Stellar did not respond. Check VPN/proxy/firewall access to the configured '
                    'Stellar hosts and ports, then verify the session provisioning with Stellar.')
        return 'Check the Stellar session provisioning, and the credentials in .env if any.'


# Native FIX session implementation.
import os
import uuid
from collections import deque
import socket
import ssl
import threading
import time
from types import SimpleNamespace

SOH = "\x01"

def fix_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H:%M:%S.%f")[:-3]

def encode_fix_message(fields: list[tuple[str, str]]) -> bytes:
    """Encode a FIX 4.4 message with exact BodyLength and CheckSum values."""
    body = SOH.join(f"{tag}={value}" for tag, value in fields) + SOH
    prefix = f"8={BEGIN_STRING}{SOH}".encode("ascii")
    middle = f"9={len(body.encode('ascii'))}{SOH}".encode("ascii") + body.encode("ascii")
    return prefix + middle + f"10={sum(prefix + middle) % 256:03d}{SOH}".encode("ascii")

def parse_fix_message(raw: str) -> dict[str, str]:
    return {tag: value for item in raw.split(SOH) if "=" in item
            for tag, value in [item.split("=", 1)]}

#: Messages that carry one of OUR orders: only FTM- and FT- ClOrdIDs go.
ORDER_MESSAGES = ('D', 'AB', 'F', 'G', 'AC', 'H')


class NativeFixSession:
    """Small threaded FIX 4.4 initiator for Stellar; no native extension needed."""
    native_fix = True
    #: After a logon that opened a gap, NEW orders wait for Stellar's resend
    #: of it (so the book is applied in Stellar's order first). If the
    #: resend never completes, the session goes ahead after this long — said
    #: in the FIX log. A close never waits.
    READY_FALLBACK_SEC = 5.0
    #: The incoming number is written at most this often (see SequenceStore).
    SAVE_IN_SEQ_SEC = 1.0

    def __init__(self, svc, state, session_name, cfg, on_execution_report=None, on_market_data=None,
                 on_security_definition=None, on_market_data_reject=None):
        self.svc, self.state, self.session_name, self.cfg = svc, state, session_name, cfg
        self.on_execution_report, self.on_market_data = on_execution_report, on_market_data
        self.on_security_definition = on_security_definition
        self.on_market_data_reject = on_market_data_reject
        self.socket = None
        self.stop_event = threading.Event()
        #: Set while OUR Logout waits for Stellar's: its Logout then is the
        #: answer, not a refusal.
        self.logging_out = False
        self.logout_answered = threading.Event()
        self.send_lock = threading.Lock()
        self.last_send_monotonic = 0.0
        #: Messages Stellar sent AHEAD of a gap, held until the gap is filled
        #: so the book sees them in Stellar's order (a FILLED never overtaken
        #: by the NEW it followed). The resend we asked for, and our TestRequest.
        self.pending: dict = {}
        self.resend_from: int = 0
        self.test_request_id = None
        self.logon_monotonic = None
        self._fallback_said = False
        self._in_saved = 0.0
        self.thread = threading.Thread(target=self._run, daemon=True)

    # -- sequence numbers ----------------------------------------------------

    def _store(self):
        if not self.cfg.get('persist'):
            return None
        return getattr(self.svc, 'sequences', None)

    def _store_key(self):
        key = getattr(self.svc, '_sequence_key', None)
        return key() if callable(key) else self.session_name

    def _save_in(self, force=False):
        store = self._store()
        if store is None:
            return
        if not force and time.monotonic() - self._in_saved < self.SAVE_IN_SEQ_SEC:
            return
        self._in_saved = time.monotonic()
        store.save(self._store_key(), in_seq=self.state.in_seq)

    def ready(self) -> bool:
        """Logged on AND what the logon showed was missed has been resent —
        or, if Stellar never completes the resend, READY_FALLBACK_SEC after
        the logon."""
        if self.state.status != 'CONNECTED':
            return False
        if not self.pending and not self.resend_from:
            return True
        if (self.logon_monotonic is not None
                and time.monotonic() - self.logon_monotonic >= self.READY_FALLBACK_SEC):
            if not self._fallback_said:
                self._fallback_said = True
                self.svc.audit.write(level='WARNING', category='FIX Session',
                                     session=self.session_name, direction='LOCAL',
                                     event='Resend from Stellar not complete', sequence='',
                                     details={'waited_sec': self.READY_FALLBACK_SEC,
                                              'from': self.resend_from})
            return True
        return False

    def start(self):
        with self.state.lock:
            self.state.status = "CONNECTING"
            self.state.error = ""
        self.thread.start()
        self.svc.audit.write(level='INFO', category='FIX Session', session=self.session_name,
                             direction='LOCAL', event='Connection attempt', sequence='',
                             details={'host': self.cfg['host'], 'port': self.cfg['port']})

    def is_running(self) -> bool:
        return self.thread.is_alive() and not self.stop_event.is_set()

    def stop(self):
        """A client-initiated Logout: 35=5 goes, Stellar's own Logout is
        waited for (`LOGOUT_WAIT_SEC`), then the socket is closed. A session
        that never logged on is simply closed."""
        logged_on = (self.socket is not None and self.state.status == "CONNECTED"
                     and not self.stop_event.is_set())
        try:
            if logged_on:
                self.logging_out = True
                self.send("5", [("58", "Client disconnect")])
                if self.thread.is_alive() and threading.current_thread() is not self.thread:
                    self.logout_answered.wait(LOGOUT_WAIT_SEC)
        except (OSError, ConnectionError, RuntimeError):
            pass
        self.stop_event.set()
        try:
            if self.socket:
                self.socket.close()
        except OSError:
            pass

    def _header(self, msg_type, seq):
        header = [("35", msg_type), ("34", str(seq)), ("49", self.cfg["sender_comp_id"]),
                  ("52", fix_timestamp()), ("56", self.cfg["target_comp_id"])]
        if self.cfg.get("target_sub_id"):
            header.append(("57", self.cfg["target_sub_id"]))
        if self.cfg.get("on_behalf_of_comp_id"):
            header.append(("115", self.cfg["on_behalf_of_comp_id"]))
        if self.cfg.get("sender_sub_id"):
            header.append(("50", self.cfg["sender_sub_id"]))
        if self.cfg.get("on_behalf_of_sub_id"):
            header.append(("116", self.cfg["on_behalf_of_sub_id"]))
        return header

    def send(self, msg_type: str, fields: list[tuple[str, str]]):
        # Session-level messages (Logon, Heartbeat, TestRequest, Resend,
        # SequenceReset, Logout) go whatever the session's business list.
        if msg_type not in ("A", "0", "1", "2", "4", "5"):
            if msg_type not in self.cfg.get('allowed_messages', ()):
                raise NotImplementedError('This message is not enabled on this FIX session')
            # Only OUR orders go out: a reviewed manual ticket (FTM-) or the
            # Algo's own (FT-). Nothing else may be sent on this session.
            if msg_type in ORDER_MESSAGES and not dict(fields).get('11', '').startswith(
                    ('FTM-', CLORDID_PREFIX + '-')):
                raise NotImplementedError('Use a reviewed manual order ticket')
            if self.state.status != 'CONNECTED':
                raise ConnectionError('FIX session is not logged on')
            # Nothing NEW until what the logon showed was missed has been
            # resent — a close is never held back.
            opening = msg_type in ('D', 'AB') and dict(fields).get('77') != 'C'
            if (opening or msg_type == 'H') and not self.ready():
                raise ConnectionError('Stellar is still resending what was missed '
                                      'before the logon — try again in a moment')
        with self.send_lock:
            with self.state.lock:
                self.state.out_seq += 1
                seq = self.state.out_seq
                self.state.outgoing_count += 1
                self.state.last_message = datetime.now(timezone.utc).isoformat()
            store = self._store()
            if store is not None:
                # Written BEFORE it goes: a number is never used twice.
                store.save(self._store_key(), out_seq=seq)
            payload = encode_fix_message(self._header(msg_type, seq) + fields)
            if not self.socket:
                raise ConnectionError("FIX socket is not connected")
            self.socket.sendall(payload)
            self.last_send_monotonic = time.monotonic()
            self.svc.log_fix(self.session_name, "OUT", msg_type, str(seq), payload.decode("ascii"))

    def _logon_fields(self):
        fields = [("98", "0"), ("108", str(self.cfg.get("heartbeat", 30)))]
        if self.cfg.get('reset_on_logon'):
            fields.append(("141", "Y"))
        password = self.cfg.get("password") or ''
        if password:
            # Stellar's spec lists no credentials on the Logon; a session
            # Stellar issued a password for sends it (553 / 554). A session
            # authorised by its address sends a bare Logon.
            username = self.cfg.get('username') or self.cfg['sender_comp_id']
            fields += [("553", username), ("554", password)]
        return fields

    def _run(self):
        try:
            self.socket = socket.create_connection((self.cfg["host"], int(self.cfg["port"])), timeout=15)
            if self.cfg.get("use_tls"):
                self.socket = ssl.create_default_context().wrap_socket(
                    self.socket, server_hostname=self.cfg["host"])
            self.socket.settimeout(1)
            store = self._store()
            saved = store.load(self._store_key()) if store is not None else {}
            with self.state.lock:
                self.state.sender_comp_id = self.cfg["sender_comp_id"]
                self.state.target_comp_id = self.cfg["target_comp_id"]
                if self.cfg.get('reset_on_logon') or store is None:
                    self.state.out_seq = 0
                    self.state.in_seq = 0
                else:
                    self.state.out_seq = int(saved.get('out_seq') or 0)
                    self.state.in_seq = int(saved.get('in_seq') or 0)
            if store is not None and self.cfg.get('reset_on_logon'):
                store.reset(self._store_key())
            if self.stop_event.is_set():
                return
            self.send("A", self._logon_fields())
            buffer = ""
            heartbeat_seconds = max(5, int(self.cfg.get("heartbeat", 30)))
            logon_deadline = time.monotonic() + 20
            self.last_receive_monotonic = time.monotonic()
            while not self.stop_event.is_set():
                try:
                    data = self.socket.recv(8192)
                    if not data:
                        raise ConnectionError("Stellar closed the FIX socket")
                    buffer += data.decode("ascii", errors="replace")
                    while f"{SOH}10=" in buffer:
                        end_marker = buffer.find(SOH, buffer.index(f"{SOH}10=") + 4)
                        if end_marker < 0:
                            break
                        end = end_marker + 1
                        raw, buffer = buffer[:end], buffer[end:]
                        self._incoming(raw)
                        self.last_receive_monotonic = time.monotonic()
                except socket.timeout:
                    pass

                self._tick()
                # FIX requires the initiator to send heartbeats when it has
                # been idle for the negotiated interval.
                if time.monotonic() - self.last_send_monotonic >= heartbeat_seconds:
                    self.send("0", [])

                if self.state.status == "CONNECTING" and time.monotonic() >= logon_deadline:
                    raise TimeoutError("Stellar did not answer the Logon request within 20 seconds")
                # Quiet for longer than a heartbeat: ask Stellar to prove it
                # is there (TestRequest) before giving up on the session.
                idle = time.monotonic() - self.last_receive_monotonic
                if (self.state.status == "CONNECTED" and idle > heartbeat_seconds + 2
                        and self.test_request_id is None):
                    self.test_request_id = f"TEST-{int(time.time())}"
                    self.send("1", [("112", self.test_request_id)])
                if idle > heartbeat_seconds * 2 + 5:
                    raise TimeoutError("Stellar heartbeat timed out; reconnect the session")
        except Exception as exc:  # show the actual server/network reason in UI
            self.svc.audit.write(level='ERROR', category='Errors', session=self.session_name,
                                 direction='LOCAL', event='Connection failed', sequence='',
                                 details={'error': self.svc._redact(str(exc))})
            with self.state.lock:
                self.state.status = "ERROR"
                self.state.error = self.svc._redact(str(exc))
        finally:
            try:
                self._save_in(force=True)
            except Exception:                             # noqa: BLE001
                pass
            try:
                if self.socket:
                    self.socket.close()
            except OSError:
                pass
            if self.state.status != "ERROR":
                self.state.status = "DISCONNECTED"

    def _tick(self) -> None:
        """Once per pass of the receive loop (about a second)."""
        self._save_in()

    def _incoming(self, raw: str):
        fields = parse_fix_message(raw)
        checksum_start = raw.rfind(f"{SOH}10=") + 1
        body_start = raw.find(SOH, raw.find(SOH) + 1) + 1
        if (fields.get('8') != BEGIN_STRING
                or int(fields.get('9', '-1')) != checksum_start - body_start
                or int(fields.get('10', '-1')) != sum(raw[:checksum_start].encode('ascii')) % 256):
            raise ValueError('Invalid FIX message length or checksum')
        if (fields.get('49') != self.cfg['target_comp_id']
                or fields.get('56') != self.cfg['sender_comp_id']):
            raise ValueError('Incoming FIX CompIDs do not match the configured session')
        msg_type, seq = fields.get("35", "?"), fields.get("34", "?")
        expected = self.state.in_seq + 1
        if seq.isdigit() and msg_type == "4" and fields.get("123") != "Y":
            # SequenceReset, Reset mode: Stellar sets the next number outright.
            self._sequence_reset(fields, raw)
            return
        if (seq.isdigit() and msg_type == "A" and int(seq) < expected
                and fields.get("141") == "Y"):
            # Stellar's Logon says it RESET its numbers: its next is this one.
            with self.state.lock:
                self.state.in_seq = int(seq) - 1
            expected = int(seq)
        if seq.isdigit() and int(seq) < expected:
            if fields.get("43") == "Y":
                # A resent copy of something already applied: logged, not
                # applied twice.
                self.svc.log_fix(self.session_name, "IN", msg_type, seq, raw)
                return
        if seq.isdigit() and int(seq) > expected and msg_type not in ("5",):
            if not self.cfg.get('persist') and msg_type != 'A':
                # Market Data: Stellar does not support a resend there. The
                # gap is said, the numbers move on, and every book is asked
                # for again — a book that missed an update is not trusted.
                self.svc.audit.write(level='WARNING', category='FIX Session',
                                     session=self.session_name, direction='LOCAL',
                                     event='Sequence gap — prices requested again',
                                     sequence=seq, details={'expected': expected})
                with self.state.lock:
                    self.state.in_seq = int(seq) - 1
                gap = getattr(getattr(self.svc, 'terminal', None), 'market_gap', None)
                if callable(gap):
                    gap()
                self._apply(fields, raw, msg_type, seq)
                return
            # A GAP: messages were missed. Ask Stellar for them
            # (ResendRequest) and hold this one until they arrive, so they
            # are applied in Stellar's order. A Logon is applied at once — the
            # session must come up to receive the resend.
            self.svc.log_fix(self.session_name, "IN", msg_type, seq, raw)
            if msg_type == "A":
                with self.state.lock:
                    self.state.status, self.state.error = "CONNECTED", ""
                    self.state.last_logon = datetime.now(timezone.utc).isoformat()
                self.logon_monotonic = time.monotonic()
                self._fallback_said = False
            self.pending[int(seq)] = raw
            if self.resend_from != expected:
                self._request_resend(expected, 0)
            return
        if not seq.isdigit() or int(seq) != expected:
            # A sequenced Logout still carries the server's actual rejection
            # reason in tag 58. Keep that evidence before reporting the gap.
            remote_reason = fields.get('58', '') if msg_type == '5' else ''
            if msg_type == '5':
                self.svc.log_fix(self.session_name, "IN", msg_type, seq, raw)
            detail = (f' Stellar Logout reason: {remote_reason}' if remote_reason else '')
            raise ConnectionError(
                f'FIX sequence mismatch on {msg_type}: expected {expected}, received {seq}. '
                f'Session stopped; verify order status in Stellar before reconnecting.{detail}')
        self._apply(fields, raw, msg_type, seq)
        # The gap is filled up to here: what was held follows, in order.
        while self.state.in_seq + 1 in self.pending:
            held = self.pending.pop(self.state.in_seq + 1)
            hf = parse_fix_message(held)
            self._apply(hf, held, hf.get("35", "?"), hf.get("34", "?"), logged=True)
        if not self.pending:
            self.resend_from = 0

    def _request_resend(self, begin: int, end: int) -> None:
        """ResendRequest (2): everything from `begin` (0 = to the latest)."""
        self.resend_from = begin
        self.svc.audit.write(level='WARNING', category='FIX Session', session=self.session_name,
                             direction='LOCAL', event='Sequence gap — resend requested',
                             sequence=str(begin), details={'from': begin, 'to': end or 'latest'})
        self.send("2", [("7", str(begin)), ("16", str(end))])

    def _sequence_reset(self, fields, raw) -> None:
        """SequenceReset (4): the next number Stellar will send is NewSeqNo
        (36), in Reset mode or as a GapFill over messages it will not resend."""
        self.svc.log_fix(self.session_name, "IN", "4", fields.get("34", ""), raw)
        try:
            new = int(fields.get("36", ""))
        except ValueError:
            return
        with self.state.lock:
            if new - 1 > self.state.in_seq or fields.get("123") != "Y":
                self.state.in_seq = new - 1
        while self.state.in_seq + 1 in self.pending:
            held = self.pending.pop(self.state.in_seq + 1)
            hf = parse_fix_message(held)
            self._apply(hf, held, hf.get("35", "?"), hf.get("34", "?"), logged=True)
        if not self.pending:
            self.resend_from = 0

    def _gap_fill(self, fields) -> None:
        """Answer Stellar's ResendRequest (2) with a SequenceReset-GapFill:
        our orders are NEVER resent — a stale order replayed is a new trade
        nobody decided — and Stellar is told to move on to our next number."""
        try:
            begin = int(fields.get("7", "1") or 1)
        except ValueError:
            begin = 1
        with self.send_lock:
            with self.state.lock:
                next_out = self.state.out_seq + 1
            header = self._header("4", begin) + [("43", "Y"), ("122", fix_timestamp())]
            payload = encode_fix_message(header + [("123", "Y"), ("36", str(next_out))])
            if not self.socket:
                return
            self.socket.sendall(payload)
            self.last_send_monotonic = time.monotonic()
            self.svc.log_fix(self.session_name, "OUT", "4", str(begin), payload.decode("ascii"))

    def _apply(self, fields, raw, msg_type, seq, logged=False):
        with self.state.lock:
            self.state.incoming_count += 1
            self.state.in_seq = int(seq) if seq.isdigit() else self.state.in_seq
            self.state.last_message = datetime.now(timezone.utc).isoformat()
        self.test_request_id = None                 # Stellar is there
        if not logged:
            self.svc.log_fix(self.session_name, "IN", msg_type, seq, raw)
        if msg_type == "4":                          # a GapFill, in sequence
            self._sequence_reset(fields, raw)
            return
        if msg_type in ('y', 'f', 'W', 'X', 'Y', 'j', '3', '8', '9'):
            self.svc.terminal.on_message(self.session_name, fields, raw)
        algo = getattr(self.svc, 'algo', None)
        if (algo is not None and self.session_name == 'Order Routing'
                and msg_type in ('8', '9', 'j', '3')):
            algo.on_message(msg_type, fields, raw)
        if msg_type == "A":
            with self.state.lock:
                self.state.status, self.state.error = "CONNECTED", ""
                self.state.last_logon = datetime.now(timezone.utc).isoformat()
            if self.logon_monotonic is None or not self.pending:
                self.logon_monotonic = time.monotonic()
            self._fallback_said = False
        elif msg_type == "5" and self.logging_out:
            # Stellar's answer to our Logout: a clean logout, not an error.
            with self.state.lock:
                self.state.status, self.state.error = "DISCONNECTED", ""
            self.logout_answered.set()
            self.stop_event.set()
        elif msg_type == "5":
            with self.state.lock:
                self.state.status = "ERROR"
                self.state.error = fields.get("58", "Stellar rejected or closed the logon")
            self.stop_event.set()
        elif msg_type == "0":
            self.state.last_heartbeat = datetime.now(timezone.utc).isoformat()
        elif msg_type == "1":
            self.send("0", [("112", fields.get("112", ""))])
        elif msg_type == "3":
            # A session-level Reject names one message of ours that Stellar
            # refused; the session itself is fine (FIX: never disconnect on a
            # Reject). Its reason reaches the page that sent it.
            from .manual_terminal import SESSION_REJECT_REASONS
            self.svc.audit.write(level='WARNING', category='FIX Session', session=self.session_name,
                                 direction='IN', event='Reject', sequence=seq,
                                 details={'ref_seq': fields.get('45', ''),
                                          'ref_msg': fields.get('372', ''),
                                          'ref_tag': fields.get('371', ''),
                                          'why': SESSION_REJECT_REASONS.get(fields.get('373', ''), ''),
                                          'reason': self.svc._redact(fields.get('58', ''))})
        elif msg_type == "2":
            self._gap_fill(fields)
        elif msg_type == "8" and self.on_execution_report:
            self.on_execution_report(fields, raw)
        elif msg_type in ("W", "X") and self.on_market_data:
            self.on_market_data(fields, raw)
        elif msg_type == "Y" and self.on_market_data_reject:
            self.on_market_data_reject(fields, raw)
        elif msg_type == "y" and self.on_security_definition:
            self.on_security_definition(fields, raw)


# ---------------------------------------------------------------------------
# The Algo's orders to Stellar, over the Order Router session
# ---------------------------------------------------------------------------

#: OrdType (40) / TimeInForce (59) for what the executor sends.
_ORD_TYPE = {'MARKET': '1', 'LIMIT': '2'}
_TIF = {'DAY': '0', 'GTC': '1', 'IOC': '3', 'FOK': '4'}
#: OrdStatus (39) -> our order state. Stellar sends 0 / 1 / 2 / 4 / 8 and
#: 9 (suspended — held: still working at the venue).
_ORD_STATUS = {'0': 'WORKING', '1': 'PARTIAL', '2': 'FILLED', '3': 'EXPIRED',
               '4': 'CANCELLED', '5': 'WORKING', '8': 'REJECTED', '9': 'WORKING',
               'C': 'EXPIRED', 'A': 'PENDING', '6': None, 'E': None}


def _fix_number(value: float) -> str:
    """A price or quantity as FIX wants it: no exponent, no float noise."""
    text = format(round(float(value), 10), 'f').rstrip('0').rstrip('.')
    return text if text not in ('', '-0') else '0'


def order_parties(venue, algorithm: bool) -> List[tuple]:
    """Stellar's Parties group (453) for an order of ours, and the MiFID
    tags beside it: 452=11 the Stellar User Name and 452=24 the Stellar
    Account Name (both REQUIRED by Stellar), 452=1000 the exchange clearing
    account 1 when set (CME / KRX), and the MiFID short codes when set —
    qualified 22 (algorithm) for the Algo, 24 (natural person) for a hand."""
    qualifier = '22' if algorithm else '24'
    rows = [(str(getattr(venue, 'username', '') or ''), 'D', '11', None),
            (str(getattr(venue, 'account', '') or ''), 'D', '24', None)]
    if getattr(venue, 'clearing_account', ''):
        rows.append((venue.clearing_account, 'D', '1000', None))
    if getattr(venue, 'mifid_client_id', ''):
        rows.append((venue.mifid_client_id, 'P', '3', '23'))
    if getattr(venue, 'mifid_execution_decision', ''):
        rows.append((venue.mifid_execution_decision, 'P', '12', qualifier))
    if getattr(venue, 'mifid_investment_decision', ''):
        rows.append((venue.mifid_investment_decision, 'P', '122', qualifier))
    return rows


def parties_fields(rows) -> List[tuple]:
    fields = [('453', str(len(rows)))]
    for party_id, source, role, qualifier in rows:
        fields += [('448', party_id), ('447', source), ('452', role)]
        if qualifier:
            fields.append(('2376', qualifier))
    return fields


def mifid_fields(venue) -> List[tuple]:
    fields = []
    if getattr(venue, 'mifid_capacity', '') in ('DEAL', 'AOTC', 'MTCH'):
        fields.append(('29', venue.mifid_capacity))
    if getattr(venue, 'mifid_dea', False):
        fields.append(('1724', '5'))
    return fields


class AlgoOrderRouter:
    """The Algo's orders to Stellar: New Order Single (D) or New Order
    Multi-leg (AB) for a listed strategy, Cancel (F), Cancel/Replace (G / AC),
    Order Status (H), and the execution reports and cancel rejects that come
    back for them — folded into `GatewayEvent`s that carry a SNAPSHOT of the
    order, never the live object.

    Every order is ours by its ClOrdID (`FT-…`) — and, for a trade, which
    Stellar sends WITHOUT a ClOrdID, by the OrderID (37) Stellar gave it.
    Anything else on the session — a manual ticket (`FTM-…`), a hand order in
    Stellar — is not read here and is never touched. A close says it is a
    close (77=C), carries the position it closes and that position's tickets
    (Stellar's ExchangeText party, 452=1002), and is capped by the executor
    at what is open.

    The ids (ClOrdID chain and Stellar's OrderID) are kept in the terminal's
    database: a cancel needs the OrderID, and an order adopted after a
    restart is found by it.

    Positions: Stellar's Order Router provides none. `positions()` is None —
    unknown, which is NOT flat — with the reason.
    """

    def __init__(self, gateway):
        self.gw = gateway
        self.lock = threading.RLock()
        self.orders: Dict[str, Dict[str, Any]] = {}     # root id -> record
        self.ids: Dict[str, str] = {}                    # any id -> root id
        self.by_order_id: Dict[str, str] = {}            # Stellar OrderID -> root id
        self.events: List[GatewayEvent] = []
        self.exec_ids = set()
        self.seq = 0
        # Unique across restarts: a venue refuses a ClOrdID it has seen today.
        self.prefix = f"{CLORDID_PREFIX}-{int(time.time() * 1000):x}"
        self.pos = {'status': 'not requested', 'why': None, 'logon': None}
        #: The FILLS TAPE: every execution report on this session that
        #: carries a fill, ours or not, as Stellar sent it. For the Account
        #: page's Fills tab only — it is never applied to the book
        #: (`_execution` does that, for OUR orders alone).
        self.tape: List[Dict[str, Any]] = []
        self.tape_ids = set()

    # -- ids -----------------------------------------------------------------

    def _next_id(self) -> str:
        """FT-<ms in hex>-<count in base 36>: short, unique across restarts,
        well inside Stellar's 32 characters."""
        self.seq += 1
        n, digits = self.seq, ''
        while n:
            n, r = divmod(n, 36)
            digits = '0123456789abcdefghijklmnopqrstuvwxyz'[r] + digits
        return f"{self.prefix}-{digits or '0'}"

    def owns(self, clordid: Optional[str]) -> bool:
        return bool(clordid) and clordid in self.ids

    def _remember(self, rec) -> None:
        """The ids an order needs after a restart: its current ClOrdID and
        Stellar's OrderID."""
        vo = rec['order']
        terminal = getattr(self.gw, 'terminal', None)
        if terminal is None or not hasattr(terminal, '_save'):
            return
        with terminal.lock:
            terminal._save('algo_ids', vo.clordid,
                           {'current': rec['current'], 'order_id': vo.venue_id or '',
                            'filled_qty': vo.filled_qty, 'avg_price': rec.get('avg')})

    def _recall(self, clordid) -> Dict[str, Any]:
        terminal = getattr(self.gw, 'terminal', None)
        if terminal is None or not hasattr(terminal, '_load'):
            return {}
        with terminal.lock:
            return terminal._load('algo_ids').get(clordid) or {}

    # -- sending -------------------------------------------------------------

    def _session(self):
        session = self.gw._sessions.get('Order Routing')
        if (session is None or session.state.status != 'CONNECTED'
                or not session.is_running()):
            return None
        return session

    def _instrument(self, contract_key):
        security_id = self.gw._contract_security_ids.get(contract_key)
        if not security_id:
            return None
        terminal = self.gw.terminal
        return (terminal.watch.get(security_id)
                or terminal.catalogue.get(security_id))

    # -- price units: Stellar's prices are the prices a trader knows ----------

    def _to_display(self, security_id, raw) -> float:
        terminal = getattr(self.gw, 'terminal', None)
        if terminal is None or not hasattr(terminal, 'to_display'):
            return float(raw)
        return terminal.to_display(security_id, raw)

    def _to_fix(self, security_id, price) -> str:
        terminal = getattr(self.gw, 'terminal', None)
        if terminal is None or not hasattr(terminal, 'to_fix'):
            return _fix_number(price)
        return terminal.to_fix(security_id, price)

    def _emit(self, kind, rec, text='', fill=None):
        from dataclasses import replace
        vo = rec['order']
        self.events.append(GatewayEvent(
            kind=kind, contract_key=vo.contract_key, clordid=vo.clordid,
            text=text, order=replace(vo), fill=fill, ts=utcnow()))

    def _refuse(self, rec, text):
        rec['order'].state = OrderState.REJECTED
        rec['order'].text = text
        self._emit('REJECTED', rec, text)

    def send(self, order: OrderRequest) -> str:
        with self.lock:
            clordid = self._next_id()
            vo = VenueOrder(clordid=clordid, contract_key=order.contract_key,
                            side=order.side, qty=order.qty, price=order.price,
                            order_type=order.order_type,
                            state=OrderState.PENDING, ts=utcnow())
            rec = {'order': vo, 'current': clordid, 'pending': None,
                   'request': order, 'security_id': None, 'avg': None}
            self.orders[clordid] = rec
            self.ids[clordid] = clordid
            instrument = self._instrument(order.contract_key)
            venue = self.gw.venue
            session = self._session()
            terminal = self.gw.terminal
            why = (None if instrument else
                   f'{order.contract_key} has no Stellar security id on this '
                   f'session — subscribe it before the Algo can trade it')
            why = why or (None if not instrument or terminal.orderable(instrument) else
                          f"Stellar has not sent {order.contract_key}'s definition "
                          f"(its Security List entry: CFI code, maturity, legs) yet")
            why = why or (None if getattr(venue, 'account', '') else
                          'no Stellar account is set on the venue (452=24)')
            why = why or (None if getattr(venue, 'username', '') else
                          'no Stellar user name is set on the venue (452=11)')
            why = why or (None if session else
                          'the Stellar Order Router is not connected')
            why = why or (None if order.order_type.value in _ORD_TYPE
                          else f'{order.order_type.value} is not an Algo '
                               f'order type here')
            if why:
                self._refuse(rec, why)
                return clordid
            rec['security_id'] = instrument['security_id']
            rec['instrument'] = instrument
            try:
                session.send(terminal.order_msg(instrument),
                             self._new_order_fields(rec, clordid))
            except Exception as e:                   # noqa: BLE001
                self._refuse(rec, f'not sent: {self.gw._redact(str(e))}')
            return clordid

    def _close_text(self, order) -> str:
        refs = ' '.join(order.close_tickets[-6:])
        return f"Close P{order.position_id or '?'} {refs}".strip()

    def _order_fields(self, rec, qty, price):
        order = rec['request']
        venue = self.gw.venue
        terminal = self.gw.terminal
        tif = getattr(order.tif, 'value', order.tif) or 'DAY'
        fields = [('21', '1')] + terminal.instrument_fields(rec['instrument'])
        fields += [('54', '1' if order.side.value == 'BUY' else '2'),
                   ('38', _fix_number(qty)),
                   ('40', _ORD_TYPE[order.order_type.value]),
                   ('59', _TIF.get(str(tif), '0')),
                   ('60', fix_timestamp())]
        if order.order_type.value == 'LIMIT' and price is not None:
            fields.append(('44', self._to_fix(rec.get('security_id'), price)))
        rows = order_parties(venue, algorithm=True)
        if order.position_effect.is_close:
            # The position it closes, and that position's venue tickets.
            rows.append((self._close_text(order)[:60], 'D', '1002', None))
        fields += parties_fields(rows)
        fields += terminal.leg_fields(rec['instrument'])
        if getattr(venue, 'send_position_effect', True):
            # OPEN or CLOSE, always said. An unknown effect is a close —
            # never an open.
            fields.append(('77', 'O' if not order.position_effect.is_close else 'C'))
        fields += mifid_fields(venue)
        return fields

    def _new_order_fields(self, rec, clordid):
        order = rec['request']
        return [('11', clordid)] + self._order_fields(rec, order.qty, order.price)

    def _cancel_fields(self, rec, new_id):
        vo = rec['order']
        order = rec['request']
        instrument = rec['instrument']
        fields = [('11', new_id), ('41', rec['current']), ('37', vo.venue_id),
                  ('21', '1'), ('38', _fix_number(vo.qty)),
                  ('54', '1' if order.side.value == 'BUY' else '2'),
                  ('60', fix_timestamp())]
        if instrument.get('exchange'):
            fields.append(('207', instrument['exchange']))
        fields += parties_fields(order_parties(self.gw.venue, algorithm=True))
        return fields

    def cancel(self, clordid: str) -> None:
        with self.lock:
            rec = self.orders.get(self.ids.get(clordid, ''))
            if rec is None or rec['order'].state.is_done or rec['pending']:
                return
            session = self._session()
            if session is None or 'instrument' not in rec:
                return
            if not rec['order'].venue_id:
                if rec.get('adopted'):
                    # A previous run's order whose OrderID was never heard:
                    # Stellar cancels by OrderID (37) only, so it cannot be
                    # cancelled from here — said, never assumed gone.
                    self._emit('CANCEL_REJECTED', rec,
                               "Stellar's OrderID for this order was never received, "
                               "so it cannot be cancelled from here — check it in Stellar")
                    return
                # Stellar cancels by its OrderID (37), which arrives with the
                # acknowledgement: the cancel goes the moment it does.
                rec['cancel_wanted'] = True
                return
            new_id = self._next_id()
            self.ids[new_id] = rec['order'].clordid
            rec['pending'] = {'id': new_id, 'kind': 'CANCEL'}
            try:
                session.send('F', self._cancel_fields(rec, new_id))
            except Exception as e:                   # noqa: BLE001
                rec['pending'] = None
                self._emit('CANCEL_REJECTED', rec,
                           f'cancel not sent: {self.gw._redact(str(e))}')

    def flush_deferred(self) -> None:
        """Cancels asked for before Stellar's OrderID was known."""
        with self.lock:
            for root, rec in list(self.orders.items()):
                if (rec.get('cancel_wanted') and rec['order'].venue_id
                        and not rec['order'].state.is_done and not rec['pending']):
                    rec['cancel_wanted'] = False
                    self.cancel(root)
                elif rec.get('cancel_wanted') and rec['order'].state.is_done:
                    rec['cancel_wanted'] = False

    def amend(self, clordid: str, price: Optional[float] = None,
              qty: Optional[float] = None) -> None:
        """Cancel/Replace (G, or AC for a strategy): the order keeps its
        lineage and its root id."""
        with self.lock:
            rec = self.orders.get(self.ids.get(clordid, ''))
            if rec is None or rec['order'].state.is_done or rec['pending']:
                return
            session = self._session()
            if session is None or 'instrument' not in rec or not rec['order'].venue_id:
                return
            new_id = self._next_id()
            self.ids[new_id] = rec['order'].clordid
            vo = rec['order']
            fields = [('11', new_id), ('41', rec['current']), ('37', vo.venue_id)]
            fields += self._order_fields(
                rec, qty if qty is not None else vo.qty,
                price if price is not None else vo.price)
            rec['pending'] = {'id': new_id, 'kind': 'REPLACE'}
            try:
                session.send(self.gw.terminal.order_msg(rec['instrument'], replace=True),
                             fields)
            except Exception as e:                   # noqa: BLE001
                rec['pending'] = None
                self._emit('CANCEL_REJECTED', rec,
                           f'replace not sent: {self.gw._redact(str(e))}')

    def adopt(self, row: Dict[str, Any]) -> bool:
        """An order of ours recorded as working when the engine last ran —
        taken back under management so it can be cancelled, and so a fill
        for it is applied to the book it belongs to."""
        clordid = str(row.get('clordid') or '')
        if not clordid.startswith(CLORDID_PREFIX + '-') or clordid in self.ids:
            return False
        from .models import Intent, OrderType as OT, PositionEffect, Side as S
        with self.lock:
            request = OrderRequest(
                contract_key=row.get('contract_key') or '',
                side=S(row.get('side') or 'BUY'), qty=float(row.get('qty') or 0),
                order_type=OT(row.get('order_type') or 'MARKET'),
                intent=Intent(row.get('intent') or 'OPEN'),
                price=row.get('price'),
                position_effect=(PositionEffect.CLOSE
                                 if (row.get('intent') or '') == 'CLOSE'
                                 else PositionEffect.OPEN))
            saved = self._recall(clordid)
            vo = VenueOrder(clordid=clordid, contract_key=request.contract_key,
                            side=request.side, qty=request.qty,
                            filled_qty=float(saved.get('filled_qty') or row.get('filled_qty') or 0),
                            venue_id=saved.get('order_id') or row.get('venue_id') or None,
                            price=request.price, order_type=request.order_type,
                            state=OrderState.WORKING, ts=utcnow())
            rec = {'order': vo, 'current': saved.get('current') or clordid,
                   'pending': None, 'request': request, 'adopted': True,
                   'avg': saved.get('avg_price')}
            instrument = self._instrument(request.contract_key)
            if instrument:
                rec['instrument'] = instrument
                rec['security_id'] = instrument['security_id']
            self.orders[clordid] = rec
            self.ids[clordid] = clordid
            self.ids[rec['current']] = clordid
            if vo.venue_id:
                self.by_order_id[vo.venue_id] = clordid
            return True

    def unheard_orders(self) -> List[str]:
        """Algo orders not in a final state whose OrderID Stellar never gave:
        asked about by Mass Order Status in the sweep."""
        with self.lock:
            return [rec['current'] for rec in self.orders.values()
                    if not rec['order'].state.is_done and not rec['order'].venue_id]

    def status_targets(self) -> List[Dict[str, Any]]:
        """Every Algo order not in a final state that Stellar can be asked
        about (it has an OrderID) — for the sweep after a logon."""
        with self.lock:
            out = []
            for rec in self.orders.values():
                vo = rec['order']
                if vo.state.is_done or not vo.venue_id:
                    continue
                extra = [('54', '1' if vo.side.value == 'BUY' else '2')]
                if rec.get('instrument'):
                    extra += self.gw.terminal.status_fields(rec['instrument'])
                out.append({'clordid': rec['current'], 'order_id': vo.venue_id,
                            'fields': extra})
            return out

    # -- what Stellar sends back ------------------------------------------------

    def on_message(self, msg_type: str, fields: Dict[str, str], raw: str):
        with self.lock:
            if msg_type == '8':
                self._record_fill(fields, raw)
                self._execution(fields)
                if fields.get('790'):
                    self.gw.status_answered(fields['790'])
                if fields.get('584') and fields.get('912', 'Y') == 'Y':
                    # The last report of a Mass Order Status answer.
                    self.gw.status_answered(fields['584'])
            elif msg_type == '9':
                self._cancel_reject(fields)
            elif msg_type in ('j', '3'):
                self._reject(fields)

    def _contract_for(self, f) -> str:
        """Which contract a report is for: ours by its order; anyone's by
        the Security ID if it carries one, else by Stellar's symbol and
        maturity against the contract's Security List entry."""
        rec = self._rec_for(f)
        if rec is not None:
            return rec['order'].contract_key
        for c in self.gw.contracts:
            if f.get('48') and str(getattr(c, 'security_id', '') or '') == f['48']:
                return getattr(c, 'key', '')
        terminal = getattr(self.gw, 'terminal', None)
        if terminal is None:
            return ''
        for c in self.gw.contracts:
            instrument = terminal.watch.get(str(getattr(c, 'security_id', '') or '')) or {}
            if terminal.matches_report(instrument, f):
                return getattr(c, 'key', '')
        return ''

    def _record_fill(self, f, raw) -> None:
        """One fill on the tape, in Stellar's own fields: 60 TransactTime,
        the account (452=24), 55/200/207 instrument, 54 side, 77 Open/Close
        if echoed, 32/31 LastQty/LastPx, 151 leaves, 37 OrderID, 17 ExecID,
        527 trade id, 11 ClOrdID (never on a trade), 58."""
        from . import stellar_exec
        if stellar_exec.kind(f) != stellar_exec.FILL:
            return                       # a strategy leg, a bust, a status: not a fill
        exec_id = f.get('17')
        try:
            qty = float(f.get('32') or 0)
            price = self._to_display(f.get('48') or '', f.get('31'))
        except (TypeError, ValueError, ArithmeticError):
            return
        if not exec_id or qty <= 0 or stellar_exec.exec_key(f) in self.tape_ids:
            return
        self.tape_ids.add(stellar_exec.exec_key(f))
        rec = self._rec_for(f)
        clordid = f.get('11') or (rec['current'] if rec else '')
        terminal = getattr(self.gw, 'terminal', None)
        algo = rec is not None or clordid.startswith(CLORDID_PREFIX + '-')
        manual = not algo and (clordid.startswith('FTM-') or bool(
            terminal is not None and hasattr(terminal, 'owns_order_id')
            and terminal.owns_order_id(f.get('37') or '')))
        parties = stellar_exec.parties(_pairs(raw))
        self.tape.append({
            'exec_id': exec_id, 'clordid': clordid,
            'orig_clordid': f.get('41') or '',
            'order_id': f.get('37') or '',
            'trade_id': f.get('527') or '',
            'exchange_trade_id': f.get('1003') or '',
            'venue_time': f.get('60') or '', 'account': parties.get('24') or f.get('1') or '',
            'security_id': f.get('48') or '',
            'symbol': ' '.join(p for p in (f.get('55'), f.get('200')) if p),
            'contract_key': self._contract_for(f),
            'side': {'1': 'BUY', '2': 'SELL'}.get(f.get('54'), f.get('54') or ''),
            'open_close': {'O': 'OPEN', 'C': 'CLOSE', 'F': 'FIFO'}.get(
                f.get('77'), f.get('77') or ''),
            'qty': qty, 'price': price,
            'cum_qty': float(f['14']) if f.get('14') else None,
            'leaves_qty': float(f['151']) if f.get('151') else None,
            'exec_type': f.get('150'), 'ord_status': f.get('39') or '',
            'text': self.gw._redact(f.get('58', '')),
            'ours': ('ALGO' if algo else 'MANUAL' if manual else ''),
            'received': utcnow().isoformat(),
        })

    def take_tape(self) -> List[Dict[str, Any]]:
        """The fills recorded since the last call, for the engine to keep."""
        with self.lock:
            out, self.tape = self.tape, []
        return out

    def _rec_for(self, fields):
        for tag in ('11', '41'):
            root = self.ids.get(fields.get(tag) or '')
            if root:
                return self.orders.get(root)
        root = self.by_order_id.get(fields.get('37') or '')
        if root and not fields.get('11'):
            # A trade carries no ClOrdID: it is ours by Stellar's OrderID.
            return self.orders.get(root)
        return None

    def _execution(self, f):
        rec = self._rec_for(f)
        if rec is None:
            return                      # not ours: a manual ticket, a hand order
        from . import stellar_exec
        vo = rec['order']
        if f.get('37') and not vo.venue_id:
            vo.venue_id = f['37']
            self.by_order_id[f['37']] = vo.clordid
        exec_type = f.get('150', '')
        what = stellar_exec.kind(f)
        if stellar_exec.is_status_answer(f):
            exec_type = 'I'         # a Mass Order Status answer's 150=5 is not a replace
        if what in (stellar_exec.BUST, stellar_exec.CORRECTION):
            # A change to an EARLIER fill: said, never booked as a new one.
            self._emit('TRADE_CHANGE', rec, stellar_exec.correction_text(f, what))
            return
        if what == stellar_exec.LEG:
            return          # a strategy leg's own fill: the strategy is booked once
        if exec_type == 'F' or what == stellar_exec.FILL:
            self._fill(rec, f)
            return
        state = _ORD_STATUS.get(f.get('39', ''))
        if what == stellar_exec.STATUS and state in ('FILLED', 'PARTIAL'):
            # A status answer's FILLED without the trade is not a fill: the
            # state follows the trades this book has seen (said below).
            state = None
        was = vo.state
        # Never moved back from a final state: a report resent by Stellar is
        # older than the FILLED or CANCELLED already applied.
        if state and not (vo.state.is_done and not OrderState(state).is_done):
            vo.state = OrderState(state)
        vo.text = self.gw._redact(f.get('58', '')) or vo.text
        vo.ts = utcnow()
        if exec_type == '5':                           # replaced
            rec['current'] = f.get('11') or rec['current']
            if f.get('44'):
                vo.price = self._to_display(rec.get('security_id'), f['44'])
            if f.get('38'):
                vo.qty = float(f['38'])
        if exec_type in ('4', '5', '8') and rec['pending']:
            rec['pending'] = None
        if exec_type == '4' and f.get('11') in self.ids:
            rec['current'] = f['11']
        self._remember(rec)
        if what == stellar_exec.STATUS:
            self._status_answer(rec, f, changed=vo.state is not was)
            return
        kind = {'0': 'ACK', '4': 'CANCELLED', '5': 'REPLACED', '8': 'REJECTED',
                'C': 'EXPIRED', '3': 'EXPIRED'}.get(exec_type)
        if kind == 'REJECTED':
            vo.text = stellar_exec.reject_reason(f, vo.text)
        if kind:
            self._emit(kind, rec, vo.text or kind.lower())

    def _fill(self, rec, f) -> None:
        """A trade (150=F): the quantity and price are Stellar's LastQty /
        LastPx; the filled total and the state are this order's own sum —
        Stellar's CumQty, OrdStatus and AvgPx on a trade are not relied on."""
        from . import stellar_exec
        vo = rec['order']
        key = stellar_exec.exec_key(f)
        try:
            qty = float(f.get('32') or 0)
            price = self._to_display(rec.get('security_id'), f.get('31'))
        except (TypeError, ValueError, ArithmeticError):
            return
        if not key or key in self.exec_ids or qty <= 0:
            return
        self.exec_ids.add(key)
        before = float(vo.filled_qty or 0)
        vo.filled_qty = before + qty
        rec['avg'] = (price if not rec.get('avg') or not before else
                      (rec['avg'] * before + price * qty) / vo.filled_qty)
        done = vo.filled_qty >= vo.qty - 1e-9 or f.get('151') == '0'
        vo.state = OrderState.FILLED if done else OrderState.PARTIAL
        vo.ts = utcnow()
        venue_ts = None
        try:
            venue_ts = datetime.strptime(f.get('60', ''),
                                         '%Y%m%d-%H:%M:%S.%f').replace(tzinfo=timezone.utc)
        except ValueError:
            pass
        fill = Fill(venue=getattr(self.gw.venue, 'name', 'Stellar'),
                    exec_id=key, clordid=vo.clordid,
                    contract_key=vo.contract_key, side=vo.side, qty=qty,
                    price=price, our_ts=utcnow(), venue_ts=venue_ts)
        self._remember(rec)
        self._emit('FILL' if vo.state is OrderState.FILLED else 'PARTIAL',
                   rec, f"filled {qty:g} @ {price:g}", fill)

    def _status_answer(self, rec, f, changed=True) -> None:
        """Stellar's answer to an Order Status Request (150=I): the order's
        state as Stellar holds it. A final state is applied; filled quantity
        this book has not seen a trade for is SAID — never booked without a
        price."""
        vo = rec['order']
        try:
            cum = float(f.get('14') or 0)
        except ValueError:
            cum = 0.0
        if cum > float(vo.filled_qty or 0) + 1e-9:
            self._emit('TRADE_CHANGE', rec,
                       f"Stellar reports {cum:g} filled on {vo.clordid} but this book has "
                       f"{float(vo.filled_qty or 0):g} — the trade report was not received; "
                       f"this program has NOT changed the position; check it in Stellar")
        kind = {'4': 'CANCELLED', '8': 'REJECTED'}.get(f.get('39', ''))
        if kind and changed:
            self._emit(kind, rec, vo.text or 'order status: ' + kind.lower())

    def _cancel_reject(self, f):
        rec = self._rec_for(f) or self.orders.get(self.by_order_id.get(f.get('37') or '', ''))
        if rec is None:
            return
        rec['pending'] = None
        state = _ORD_STATUS.get(f.get('39', ''))
        if state and not (rec['order'].state.is_done and not OrderState(state).is_done):
            rec['order'].state = OrderState(state)
        self._emit('CANCEL_REJECTED', rec,
                   self.gw._redact(f.get('58', 'Stellar refused the change')))

    def _reject(self, f):
        """A business (j) or session (3) reject: ours if it names one of our
        ids."""
        ref = f.get('379') or ''
        root = self.ids.get(ref)
        if root:
            rec = self.orders[root]
            self._refuse(rec, self.gw._redact(f.get('58', 'Stellar rejected it')))

    # -- positions -------------------------------------------------------------

    NO_POSITIONS_WHY = ("Stellar's FIX Order Router does not provide positions — "
                        "check them in Stellar or on the clearer's statement")

    def request_positions(self, now_monotonic: float) -> None:
        """Stellar has no positions request: unknown, said once per logon."""
        session = self._session()
        if session is None or not getattr(session, 'ready', lambda: True)():
            return
        logon = session.state.last_logon
        with self.lock:
            if self.pos['logon'] != logon:
                self.pos.update(logon=logon, status='unavailable', why=self.NO_POSITIONS_WHY)

    def positions(self) -> Optional[List[VenuePosition]]:
        return None

    def positions_status(self) -> Dict[str, Any]:
        with self.lock:
            return {'status': self.pos['status'], 'why': self.pos['why']}

    def drain(self) -> List[GatewayEvent]:
        with self.lock:
            out, self.events = self.events, []
        return out


def _pairs(raw: str):
    for item in raw.split(SOH):
        if '=' in item:
            tag, value = item.split('=', 1)
            yield tag, value
