"""Manual TT UAT instrument, quote and order workflows.

Runs in the engine and uses the gateway's existing sessions. The strategy
gateway remains separate: manual orders never become algo-owned positions.
"""
import copy
import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation

from . import slippage as slippage_mod


ORDER_TYPES = {'MARKET': '1', 'LIMIT': '2', 'STOP': '3', 'STOP_LIMIT': '4',
               'MARKET_ON_CLOSE': '5', 'LIMIT_ON_CLOSE': 'B', 'POST_ONLY': 'p'}
TIFS = {'DAY': '0', 'GTC': '1', 'AT_OPEN': '2', 'IOC': '3', 'FOK': '4', 'GTD': '6', 'AT_CLOSE': '7'}
#: MDReqRejReason (281): why TT refused a price request, in words.
MD_REJECT_REASONS = {'0': 'unknown symbol', '1': 'duplicate request id',
                     '2': 'insufficient bandwidth', '3': 'no market-data permission for this product',
                     '4': 'unsupported subscription type', '5': 'unsupported market depth',
                     '6': 'unsupported update type', '7': 'unsupported aggregated book',
                     '8': 'unsupported entry type'}
#: SessionRejectReason (373) on a Session-Level Reject (3), in words.
SESSION_REJECT_REASONS = {'0': 'invalid tag number', '1': 'required tag missing',
                          '2': 'tag not defined for this message type', '3': 'undefined tag',
                          '4': 'tag specified without a value', '5': 'incorrect value',
                          '6': 'incorrect data format', '7': 'decryption problem',
                          '8': 'signature problem', '9': 'CompID problem',
                          '10': 'sending time accuracy problem', '11': 'invalid message type',
                          '99': 'other'}
LIMIT_TYPES = {'LIMIT', 'STOP_LIMIT', 'LIMIT_ON_CLOSE', 'POST_ONLY'}
TERMINAL = {'FILLED', 'CANCELED', 'REJECTED', 'EXPIRED'}
DEFAULT_RISK = {'trading_enabled': True, 'max_order_qty': 0.0,
                'max_open_qty_per_instrument': 0.0, 'daily_loss_limit': 0.0}


def now():
    return datetime.now(timezone.utc).isoformat()


def fix_time():
    return datetime.now(timezone.utc).strftime('%Y%m%d-%H:%M:%S.%f')[:-3]


def clean(value, name='value', required=False):
    text = str(value or '').strip()
    if (required and not text) or len(text) > 240 or any(ord(c) < 32 or ord(c) > 126 for c in text) or '|' in text:
        raise ValueError(f'{name} is missing or contains invalid characters')
    return text


def number(value, name, positive=False):
    try:
        d = Decimal(str(value))
        if not d.is_finite() or (positive and d <= 0):
            raise ValueError()
        return format(d, 'f')
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f'{name} must be a finite' + (' positive' if positive else '') + ' number') from None


def pairs(raw):
    return [tuple(p.split('=', 1)) for p in raw.split('\x01') if '=' in p]


#: What the instrument explorer is sent per cached contract. Enough to name,
#: group and add it; the full definition stays here until it is added.
CATALOGUE_FIELDS = ('security_id', 'symbol', 'exchange', 'security_type',
                    'display_name', 'maturity', 'tt_name', 'product_key',
                    'structure', 'first_month', 'leg_months', 'description', 'contract_code',
                    'tick_size', 'tick_value', 'point_value', 'currency')

MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
          'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')

#: What TT's own instrument explorer calls each FIX SecurityType.
TYPE_LABELS = {'FUT': 'Future', 'MLEG': 'Spread', 'OPT': 'Option',
               'CS': 'Stock', 'FOR': 'Forward', 'SPOT': 'Spot'}


def month_label(value):
    """`202611` or `20261120` as TT writes it, `Nov26`; anything else as is."""
    text = str(value or '')
    if len(text) >= 6 and text[:6].isdigit() and 1 <= int(text[4:6]) <= 12:
        return MONTHS[int(text[4:6]) - 1] + text[2:4]
    return text


def leg_label(leg):
    """One leg the way TT names it: `+1xCL Nov26`."""
    sign = {'1': '+', '2': '-'}.get(leg.get('624', ''), '')
    ratio = leg.get('623') or '1'
    try:
        ratio = format(Decimal(ratio).normalize(), 'f')
    except InvalidOperation:
        pass
    month = month_label(leg.get('610') or leg.get('611', ''))
    return f"{sign}{ratio}x{leg.get('600', '?')} {month}".strip()


def tt_name(instrument):
    """The name TT's explorer shows. A spread is named by its LEGS —
    `+1xCL Nov26:-1xBZ Nov26` — because the product symbol and the exchange's
    contract code (`CLX6-CLJ0`) do not say which way round it is."""
    legs = instrument.get('legs') or []
    if legs:
        return ':'.join(leg_label(leg) for leg in legs)
    month = month_label(instrument.get('maturity', ''))
    return ' '.join(p for p in (instrument.get('symbol', ''), month) if p)


def product_key(instrument):
    """The product a spread is listed under in TT: its leg products in leg
    order, `CL|BZ`, or `CL` for a calendar. The FIX symbol alone files every
    inter-commodity spread under one of its legs."""
    products = []
    for leg in instrument.get('legs') or []:
        symbol = leg.get('600')
        if symbol and symbol not in products:
            products.append(symbol)
    return '|'.join(products) or instrument.get('symbol', '')


def structure(instrument):
    legs = instrument.get('legs') or []
    if not legs:
        return TYPE_LABELS.get(instrument.get('security_type', ''),
                               instrument.get('security_type', ''))
    single = len({leg.get('600') for leg in legs}) == 1
    if len(legs) == 2:
        return 'Calendar' if single else 'Inter-commodity'
    if len(legs) == 3 and single:
        return 'Butterfly'
    if len(legs) == 4 and single:
        return 'Condor'
    return f'{len(legs)}-leg strategy'


class ManualTerminal:
    def __init__(self, gateway, path=':memory:'):
        self.gateway = gateway
        self.lock = threading.RLock()
        self.quote_changed = threading.Event()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute('CREATE TABLE IF NOT EXISTS manual_state (kind TEXT, id TEXT, data TEXT, PRIMARY KEY(kind,id))')
        self.db.execute('CREATE TABLE IF NOT EXISTS manual_fills (id TEXT PRIMARY KEY, data TEXT)')
        self.db.commit()
        self.instruments = {}
        self.catalogue = {}
        self.search = {'status': 'Idle', 'request_id': '', 'error': ''}
        #: Every search whose answers are still wanted. An inter-commodity
        #: product is searched once per leg product, and the second search
        #: must not discard the first one's definitions as they arrive.
        self._search_ids = set()
        #: Set by the engine: returns why a NEW manual order on this ticket's
        #: instrument is refused (its Algo is TRADING it), or None. Checked
        #: here, in the terminal, so no page or command can go round it.
        #: Closes and cancels never ask.
        self.mode_block = lambda ticket=None: None
        #: Ticks THROUGH the touch a MARKET ticket goes as a limit (IOC), by
        #: Security ID — set by the engine from the contract's settings. 0 is
        #: a true market order.
        self.market_ticks = lambda security_id: 0.0
        #: The contract's "Book no wider than" in ticks (0 = no limit) — the
        #: engine's, for an instrument on the desk.
        self.spread_limit = lambda security_id: 0.0
        self.watch = self._load('watch')
        for instrument in self.watch.values():
            self._enrich(instrument)
            self.catalogue[instrument['security_id']] = copy.deepcopy(instrument)
        self.orders = self._load('order')
        saved_risk = self._load('risk').get('manual', {})
        self.risk = dict(DEFAULT_RISK)
        self.risk.update({key: saved_risk[key] for key in DEFAULT_RISK if key in saved_risk})
        self.previews = {}
        self.subscriptions = {}
        self.books = {}
        self.errors = []
        self._md_session = None
        self._or_session = None
        self._search_count = 0
        #: Instruments whose definition was asked for by Security ID this
        #: Market Data session — once each.
        self._definition_asked = set()
        self._units_known = frozenset()
        # A local record after a restart cannot establish the venue's order state.
        for order in self.orders.values():
            if order['status'] not in TERMINAL:
                order['status'] = 'UNKNOWN'
                order['text'] = 'Restarted: venue status not recovered. Verify in TT before placing another order.'
                self._save('order', order['id'], order)

    def _load(self, kind):
        return {key: json.loads(data) for key, data in self.db.execute('SELECT id,data FROM manual_state WHERE kind=?', (kind,))}

    def _enrich(self, instrument):
        p = instrument.get('parameters', {})
        instrument['tick_size'] = p.get('16552') or instrument.get('tick_size', '')
        # TT commonly supplies point value in 16554; standard FIX contract
        # multiplier (231) is the equivalent fallback when 16554 is absent.
        instrument['point_value'] = p.get('16554') or instrument.get('multiplier') or p.get('231', '')
        # TT's DisplayFactor (9787): FIX price x factor = the price a trader
        # knows (CL 9057 x 0.01 = 90.57). Where TT gives no 9787 but gives
        # both its tick in FIX units (969) and the exchange's tick (16552),
        # their ratio is the same factor — taken only when it is a power of
        # ten other than 1, the one case it cannot be a coincidence.
        instrument['display_factor'] = p.get('9787', '') or instrument.get('display_factor', '')
        instrument['display_factor_source'] = '9787' if p.get('9787') else ''
        if not instrument['display_factor'] and p.get('969') and p.get('16552'):
            try:
                ratio = Decimal(p['16552']) / Decimal(p['969'])
                exp = ratio.log10()
                if ratio != 1 and exp == exp.to_integral_value():
                    instrument['display_factor'] = format(ratio.normalize(), 'f')
                    instrument['display_factor_source'] = '16552/969'
            except (InvalidOperation, ValueError, ZeroDivisionError):
                pass
        if not instrument['display_factor'] and p:
            # A definition that says nothing of a factor: TT's prices for it
            # are already the prices a trader knows.
            instrument['display_factor'] = '1'
            instrument['display_factor_source'] = 'none given by TT'
        if p.get('969') and instrument['display_factor']:
            # TT's own tick (969) is in FIX units — the units of the prices TT
            # sends — so the ticket's tick, in trader prices, is 969 x the
            # factor. ALWAYS, when both are known: TT's 16552 is not reliably
            # in trader prices (ES on UAT: 969=25, 16552=25, 9787=0.01 — a
            # tick of 25 refused every price a trader types).
            try:
                instrument['tick_size'] = format((Decimal(p['969']) * Decimal(
                    instrument['display_factor'])).normalize(), 'f')
            except (InvalidOperation, ValueError):
                pass
        elif (p.get('16552') and instrument['display_factor_source'] == '9787'
              and instrument['display_factor'] not in ('', '1')):
            # No 969, but TT says the factor (9787): its exchange tick (16552)
            # is then in the same FIX units as its prices — GC on UAT sends
            # 16552=1 with 9787=0.1, and a tick of 1 refused every price that
            # was not a whole dollar. The trader's tick is 16552 x the factor.
            try:
                instrument['tick_size'] = format((Decimal(p['16552']) * Decimal(
                    instrument['display_factor'])).normalize(), 'f')
            except (InvalidOperation, ValueError):
                pass
        instrument['tick_value'] = ''
        if p.get('16552') and p.get('16554'):
            # The exchange's tick (16552) and its value per unit (16554) are
            # a PAIR in the same units — TT's FIX units on UAT (GC: 16552=1,
            # 16554=10; a FIX price of 42219 is 4221.9). Their product is the
            # money per tick whatever the units; read against the trader's
            # tick (0.1) the money per POINT is 100, not 10.
            try:
                value = Decimal(p['16552']) * Decimal(p['16554'])
                instrument['tick_value'] = format(value, 'f')
                if instrument.get('tick_size') and Decimal(instrument['tick_size']):
                    instrument['point_value'] = format(
                        (value / Decimal(instrument['tick_size'])).normalize(), 'f')
            except (InvalidOperation, ValueError, ZeroDivisionError):
                instrument['tick_value'] = ''
        if not instrument['tick_value'] and instrument.get('tick_size') and instrument['point_value']:
            instrument['tick_value'] = format(Decimal(instrument['tick_size']) * Decimal(instrument['point_value']), 'f')
        instrument['contract_code'] = instrument.get('contract_code') or p.get('455', '')
        instrument['display_name'] = instrument.get('contract_code') or (instrument.get('description', instrument.get('symbol', '')) + ' ' + instrument.get('maturity', ''))
        instrument['tt_name'] = tt_name(instrument)
        instrument['product_key'] = product_key(instrument)
        instrument['structure'] = structure(instrument)
        legs = instrument.get('legs') or []
        months = [(leg.get('610') or leg.get('611', ''))[:6] for leg in legs]
        instrument['first_month'] = months[0] if months else instrument.get('maturity', '')
        #: Every leg's month in leg order, so `Nov26:Dec26` sorts before
        #: `Nov26:Jan27` by date rather than by the month's spelling.
        instrument['leg_months'] = ','.join(months) or instrument.get('maturity', '')

    # -- price units: TT's FIX prices <-> the prices a trader knows ----------

    def price_factor(self, key):
        """This instrument's display factor as a Decimal, or None where TT has
        not said — then prices stay in TT's FIX units, and the screen says so."""
        instrument = self.watch.get(key) or self.catalogue.get(key) or {}
        try:
            factor = Decimal(str(instrument.get('display_factor') or ''))
        except (InvalidOperation, ValueError):
            return None
        return factor if factor.is_finite() and factor > 0 else None

    def to_display(self, key, raw):
        """A price as TT sent it (FIX units) -> the price a trader knows."""
        factor = self.price_factor(key) or Decimal(1)
        return float(Decimal(str(raw)) * factor)

    def to_fix(self, key, price):
        """A price a trader typed -> TT's FIX units, exact (tags 44 / 99)."""
        factor = self.price_factor(key) or Decimal(1)
        value = (Decimal(str(price)) / factor).normalize()
        text = format(value, 'f')
        return '0' if text in ('-0', '') else text

    def _migrate_units(self):
        """Orders recorded before prices were converted carry TT's FIX
        units: once the factor is known, bring them to trader prices — once."""
        known = frozenset(k for k in {o['ticket'].get('security_id') for o in self.orders.values()}
                          if self.price_factor(k) is not None)
        if known <= self._units_known:
            return                       # nothing new to bring over since last pass
        self._units_known = known
        for order in self.orders.values():
            if order.get('units') == 'display':
                continue
            key = order['ticket'].get('security_id')
            factor = self.price_factor(key)
            if factor is None:
                continue
            f = float(factor)
            for holder, field in ((order, 'avg_price'), (order['ticket'], 'price'),
                                  (order['ticket'], 'stop_price'),
                                  (order.get('decision') or {}, 'price'),
                                  (order.get('decision') or {}, 'bid'),
                                  (order.get('decision') or {}, 'ask')):
                if holder.get(field) not in (None, ''):
                    try:
                        value = float(holder[field]) * f
                    except (TypeError, ValueError):
                        continue
                    holder[field] = (format(Decimal(str(holder[field])) * factor, 'f')
                                     if isinstance(holder[field], str) else value)
            order['units'] = 'display'
            self._save('order', order['id'], order)
        for exec_id, data in self.db.execute('SELECT id, data FROM manual_fills').fetchall():
            fill = json.loads(data)
            order = self.orders.get(fill.get('order_id'))
            factor = order and self.price_factor(order['ticket'].get('security_id'))
            if fill.get('units') == 'display' or not factor:
                continue
            if fill.get('price') not in (None, ''):
                fill['price'] = float(Decimal(str(fill['price'])) * factor)
                fill.update(self._fill_slippage(order, fill['price'], fill.get('quantity')))
            fill['units'] = 'display'
            self.db.execute('UPDATE manual_fills SET data=? WHERE id=?', (json.dumps(fill), exec_id))
        self.db.commit()

    def _request_definition(self, key):
        """Ask TT for one instrument's definition by its Security ID — for a
        configured contract whose display factor was never received."""
        if key in self._definition_asked:
            return
        self._definition_asked.add(key)
        instrument = self.watch.get(key) or {}
        request_id = 'DEF-' + uuid.uuid4().hex[:16]
        self._search_ids.add(request_id)
        fields = [('320', request_id), ('321', '3'), ('48', key), ('22', '96'),
                  ('17000', 'Y')]
        if instrument.get('exchange'):
            fields.append(('207', instrument['exchange']))
        if instrument.get('symbol'):
            fields.append(('55', instrument['symbol']))
        try:
            self._send('Market Data', 'c', fields)
        except Exception as error:                          # noqa: BLE001
            self.errors = (self.errors + [str(error)])[-10:]

    def _save(self, kind, key, value):
        self.db.execute('INSERT OR REPLACE INTO manual_state VALUES (?,?,?)', (kind, key, json.dumps(value)))
        self.db.commit()

    def set_risk(self, args):
        """Persist operator limits. Zero means unlimited, never a guessed limit."""
        with self.lock:
            updated = {'trading_enabled': args.get('trading_enabled') is True}
            for key, label in (('max_order_qty', 'Maximum order quantity'),
                               ('max_open_qty_per_instrument', 'Maximum open quantity'),
                               ('daily_loss_limit', 'Daily loss limit')):
                try:
                    value = Decimal(str(args.get(key, 0) or 0))
                except (InvalidOperation, ValueError, TypeError):
                    raise ValueError(label + ' must be a finite non-negative number') from None
                if not value.is_finite() or value < 0:
                    raise ValueError(label + ' must be a finite non-negative number')
                updated[key] = float(value)
            self.risk = updated
            self._save('risk', 'manual', self.risk)
            return {'ok': True, 'risk': copy.deepcopy(self.risk)}

    def _risk_check(self, ticket, risk_reducing=False, exclude_order_id=None):
        """Fail closed for new exposure; closing orders remain available."""
        if risk_reducing:
            return
        if not self.risk['trading_enabled']:
            raise ValueError('Manual trading kill switch is ON. New orders are blocked; closes and cancels remain available.')
        quantity = Decimal(ticket['quantity'])
        maximum = Decimal(str(self.risk['max_order_qty']))
        if maximum and quantity > maximum:
            raise ValueError(f'Quantity exceeds the manual per-order limit of {maximum:g}')
        maximum_open = Decimal(str(self.risk['max_open_qty_per_instrument']))
        if maximum_open:
            security_id = ticket['security_id']
            exposure = Decimal('0')
            for order in self.orders.values():
                if (order['id'] == exclude_order_id or order.get('close_of')
                        or order['ticket'].get('security_id') != security_id):
                    continue
                filled = Decimal(str(order.get('filled_qty') or 0))
                closed_or_reserved = Decimal('0')
                for child in self.orders.values():
                    if child.get('close_of') != order['id']:
                        continue
                    closed_or_reserved += (Decimal(str(child.get('filled_qty') or 0))
                                           if child['status'] in TERMINAL
                                           else Decimal(child['ticket']['quantity']))
                exposure += max(Decimal('0'), filled - closed_or_reserved)
                if order['status'] in ('PENDING', 'NEW', 'PARTIALLY_FILLED', 'REPLACED'):
                    exposure += Decimal(str(order.get('remaining_qty') or 0))
            if exposure + quantity > maximum_open:
                raise ValueError(f'Order would exceed the manual open/working quantity limit of {maximum_open:g} for this instrument')
        loss_limit = Decimal(str(self.risk['daily_loss_limit']))
        if loss_limit:
            snapshot = self._pnl_snapshot([])
            today = datetime.now(timezone.utc).date()
            values = [trade['realized_pnl'] for trade in snapshot['trades']
                      if datetime.fromisoformat(trade['closed_at']).date() == today]
            if any(value is None for value in values):
                raise ValueError('Daily loss limit is enabled but realized P&L is unavailable; new exposure is blocked')
            pnl = sum(values, 0.0)
            if Decimal(str(pnl)) <= -loss_limit:
                raise ValueError(f'Daily loss limit of {loss_limit:g} has been reached; new exposure is blocked')

    def session(self, name):
        session = self.gateway._sessions.get(name)
        if session is None or session.state.status != 'CONNECTED' or not session.is_running():
            raise ConnectionError(name + ' FIX is not connected')
        return session

    def _send(self, name, msg, fields):
        self.session(name).send(msg, fields)

    def instrument_fields(self, instrument):
        # Exact TT ID is authoritative, including exchange-listed spreads.
        fields = [('55', instrument['symbol']), ('48', instrument['security_id']), ('22', '96')]
        if instrument.get('exchange'):
            fields.append(('207', instrument['exchange']))
        return fields

    def lookup(self, args):
        with self.lock:
            self.session('Market Data')
            exchange = clean(args.get('exchange'), 'Exchange', True)
            # TT names an inter-product spread product with a bar — `CL|BZ`
            # is its own product in TT's explorer — so the bar is allowed in
            # a PRODUCT SYMBOL and nowhere else. It is not a FIX delimiter
            # (SOH is, and control characters are still refused).
            symbol = '|'.join(clean(part, 'Product symbol', True)
                              for part in str(args.get('symbol') or '').split('|'))
            security_type = clean(args.get('security_type', 'FUT'))
            if security_type not in ('FUT', 'MLEG', 'OPT', 'CS', 'FOR', 'SPOT'):
                raise ValueError('Unsupported instrument type')
            maturity = clean(args.get('maturity', ''))
            if maturity and (len(maturity) != 6 or not maturity.isdigit()):
                raise ValueError('Expiry month must be YYYYMM')
            if self._search_count >= 20:
                raise ValueError('20 active catalogue searches reached. Reconnect Market Data before searching again.')
            request_id = 'SEC-' + uuid.uuid4().hex[:16]
            self.search = {'status': 'Searching', 'request_id': request_id, 'error': '', 'started': time.time(),
                           'symbol': symbol, 'security_type': security_type}
            if args.get('append'):
                self._search_ids.add(request_id)
            else:
                self.instruments.clear()
                self._search_ids = {request_id}
            fields = [('320', request_id), ('321', '3'), ('207', exchange), ('55', symbol), ('167', security_type), ('17000', 'Y')]
            if maturity:
                fields.append(('200', maturity))
            self._send('Market Data', 'c', fields)
            self._search_count += 1
            return {'ok': True, 'request_id': request_id}

    def add(self, args):
        with self.lock:
            key = clean(args.get('security_id'), 'TT Security ID', True)
            instrument = self.instruments.get(key) or self.watch.get(key) or self.catalogue.get(key)
            if not instrument:
                raise ValueError('Find and select the exact instrument from TT first')
            if len(self.watch) >= 30 and key not in self.watch:
                raise ValueError('Watchlist limit is 30 instruments')
            self.watch[key] = copy.deepcopy(instrument)
            if key in self.subscriptions:
                self.watch[key]['full_depth'] = bool(self.books.get(key, {}).get('full_depth', self.watch[key].get('full_depth')))
            self._save('watch', key, self.watch[key])
            self._subscribe(key)
            return {'ok': True, 'security_id': key}

    def remove(self, args):
        with self.lock:
            key = str(args.get('security_id', ''))
            if key in self.subscriptions:
                self._subscribe(key, '2')
            self.watch.pop(key, None)
            self.books.pop(key, None)
            self.db.execute('DELETE FROM manual_state WHERE kind=? AND id=?', ('watch', key))
            self.db.commit()
            return {'ok': True}

    def _subscribe(self, key, action='1'):
        if key in self.subscriptions and action == '1':
            return
        request_id = self.subscriptions.get(key) or 'MD-' + uuid.uuid4().hex[:16]
        fields = [('262', request_id), ('263', action), ('264', '0' if self.watch[key].get('full_depth') else '1'), ('265', '1'), ('266', 'Y'),
                  ('146', '1')] + self.instrument_fields(self.watch[key])
        if action != '2':
            fields += [('267', '4'), ('269', '0'), ('269', '1'), ('269', '2'), ('269', 'x')]
            self.subscriptions[key] = request_id
            self.books[key] = {'bid': None, 'ask': None, 'last': None, 'bid_size': None, 'ask_size': None,
                               'timestamp': '', 'error': '', 'entries': {}, 'full_depth': self.watch[key].get('full_depth', False),
                               # When the request went, and whether TT has answered it at all.
                               'requested_at': time.time(), 'answered': False}
        try:
            self._send('Market Data', 'V', fields)
        except Exception:
            self.subscriptions.pop(key, None)
            raise
        if action == '2':
            self.subscriptions.pop(key, None)

    def depth(self, args):
        with self.lock:
            key = str(args.get('security_id', ''))
            if key not in self.watch:
                raise ValueError('Add this instrument to the watchlist first')
            self.session('Market Data')
            if key in self.subscriptions:
                self._subscribe(key, '2')
            self.watch[key]['full_depth'] = args.get('enabled') is True
            self._save('watch', key, self.watch[key])
            self._subscribe(key)
            return {'ok': True}

    def poll(self):
        with self.lock:
            session = self.gateway._sessions.get('Market Data')
            if session and session.state.status == 'CONNECTED' and session.is_running():
                if session is not self._md_session:
                    self._md_session = session
                    self._search_count = 0
                    self._definition_asked = set()
                    self.subscriptions.clear()
                    self.books.clear()
                    feed = getattr(self.gateway, 'algo_feed', None)
                    if feed is not None:
                        feed.clear()
                # TT: wait for News "Recovery is complete" before sending
                # any request on Market Data (a short fallback if it never
                # comes).
                ready = getattr(session, 'ready', lambda: True)()
                for key in (self.watch if ready else ()):
                    if key not in self.subscriptions:
                        try:
                            self._subscribe(key)
                        except Exception as error:
                            self.errors = [str(error)]
                            break
                    if self.price_factor(key) is None:
                        self._request_definition(key)
                self._migrate_units()
            elif self._md_session is not None:
                self._md_session = None
                self.subscriptions.clear()
                self.books.clear()
                feed = getattr(self.gateway, 'algo_feed', None)
                if feed is not None:
                    feed.clear()
            order_session = self.gateway._sessions.get('Order Routing')
            if order_session is not self._or_session:
                if self._or_session is not None:
                    for order in self.orders.values():
                        if order['status'] not in TERMINAL:
                            order['status'] = 'UNKNOWN'
                            order['text'] = 'Session changed; verify order status in TT. No automatic resend.'
                            self._save('order', order['id'], order)
                self._or_session = order_session
            if order_session is not None and order_session.state.status in ('ERROR', 'DISCONNECTED'):
                for order in self.orders.values():
                    if order['status'] not in TERMINAL and order['status'] != 'UNKNOWN':
                        order['status'] = 'UNKNOWN'
                        order['text'] = 'FIX connection lost; venue order status is unknown. Verify in TT.'
                        self._save('order', order['id'], order)
            if self.search['status'] == 'Searching' and time.time() - self.search.get('started', 0) > 20:
                self.search['status'] = 'No results yet' if not self.instruments else 'Receiving results'

    def on_message(self, name, fields, raw):
        with self.lock:
            msg = fields.get('35')
            if msg == 'd' and fields.get('320') in self._search_ids:
                # Repeating-group fields must not overwrite the top-level identity.
                first = {}
                for tag, value in pairs(raw):
                    first.setdefault(tag, value)
                key = first.get('48')
                if key and first.get('55') and len(self.instruments) < 2000:
                    instrument = {'security_id': key, 'symbol': first['55'], 'exchange': first.get('207', first.get('100', '')),
                        'security_type': first.get('167', ''), 'maturity': first.get('200', ''),
                        'description': first.get('107', ''), 'currency': first.get('15', ''),
                        'tick_size': first.get('969', ''), 'multiplier': first.get('231', ''),
                        'strike': first.get('202', ''), 'put_call': first.get('201', ''),
                        'subtype': first.get('762', ''), 'expiry_date': first.get('541', ''),
                        'min_qty': first.get('16460', ''), 'legs': [],
                        'parameters': {k: v for k, v in first.items() if k not in ('95', '96', '553', '554')}}
                    leg = None
                    alternate = None
                    for tag, value in pairs(raw):
                        if tag == '455' and leg is None:
                            alternate = value
                        elif tag == '456' and value == '98' and alternate is not None and leg is None:
                            instrument['contract_code'] = alternate
                        if tag == '600':
                            if leg:
                                instrument['legs'].append(leg)
                            leg = {tag: value}
                        elif leg is not None and tag in ('602', '603', '608', '609', '610', '611', '612', '623', '624', '616'):
                            leg[tag] = value
                    if leg:
                        instrument['legs'].append(leg)
                    self._enrich(instrument)
                    self.instruments[key] = instrument
                    if len(self.catalogue) < 5000 or key in self.catalogue:
                        self.catalogue[key] = copy.deepcopy(instrument)
                    stale_subscription = False
                    if key in self.watch:
                        instrument['full_depth'] = self.watch[key].get('full_depth', False)
                        if (instrument.get('display_factor') or '') != (
                                self.watch[key].get('display_factor') or ''):
                            # Prices read before the factor was known are in
                            # TT's FIX units: drop them and ask for a fresh
                            # snapshot, never show 9050 for 90.50.
                            stale_subscription = key in self.subscriptions
                            self.books.pop(key, None)
                        self.watch[key] = copy.deepcopy(instrument)
                        self._save('watch', key, instrument)
                        if stale_subscription:
                            try:
                                self._subscribe(key, '2')
                                self._subscribe(key)
                            except Exception as error:      # noqa: BLE001
                                self.errors = (self.errors + [str(error)])[-10:]
                    self.search['status'] = 'Receiving results'
                elif fields.get('58'):
                    self.search['error'] = self.gateway._redact(fields['58'])
            elif msg in ('W', 'X'):
                self._market(fields, raw)
                self.quote_changed.set()
            elif msg in ('Y', 'j', '3'):
                reason = self.gateway._redact(fields.get('58', '') or '')
                code = (SESSION_REJECT_REASONS.get(fields.get('373', '')) if msg == '3'
                        else MD_REJECT_REASONS.get(fields.get('281', '')))
                if code and msg == '3' and fields.get('371'):
                    code += f" (tag {fields['371']})"
                if code:
                    reason = f"{code}" + (f" — {reason}" if reason else '')
                reason = reason or 'TT rejected the request'
                self.errors = (self.errors + [reason])[-10:]
                request = fields.get('262') or fields.get('379')
                matched = False
                for key, request_id in self.subscriptions.items():
                    if request == request_id:
                        self.books[key]['error'] = reason
                        matched = True
                if not matched and name == 'Market Data' and fields.get('372') == 'V':
                    # A session-level reject of a price request names its
                    # message, not its request: every request TT has not
                    # answered yet is the one it refused.
                    for key in self.subscriptions:
                        book = self.books.get(key) or {}
                        if not book.get('answered'):
                            book['error'] = reason
                if self.search['status'] in ('Searching', 'Receiving results'):
                    self.search['error'] = reason
            elif msg in ('8', '9'):
                self._execution(fields)

    def _market(self, fields, raw):
        received_ms = time.time_ns() / 1_000_000
        header = {}
        groups, group = [], None
        in_groups = False
        for tag, value in pairs(raw):
            if tag == '268':
                in_groups = True
                continue
            if not in_groups:
                header[tag] = value
                continue
            if tag == '279' or (tag == '269' and group and '269' in group):
                if group:
                    groups.append(group)
                group = {}
            if tag == '269' and group is None:
                group = {}
            if group is not None and tag != '10':
                group[tag] = value
        if group:
            groups.append(group)
        request_key = next((k for k, v in self.subscriptions.items() if v == header.get('262')), None)
        # An answer is an answer even with nothing in it: an empty snapshot
        # is a market nobody is quoting, not a request TT ignored.
        for answered in {request_key, header.get('48')}:
            if answered in self.books:
                self.books[answered]['answered'] = True
        reset = set()
        touched = set()
        inherited_key = None
        for group_index, entry in enumerate(groups):
            key = entry.get('48') or header.get('48') or request_key or inherited_key
            if key is None and entry.get('278'):
                matches = [k for k,b in self.books.items() if entry['278'] in b['entries']]
                if len(matches) == 1:
                    key = matches[0]
            if key not in self.books:
                continue  # Never route by product name: multiple expiries share tag 55.
            inherited_key = key
            book = self.books[key]
            book['answered'] = True
            touched.add(key)
            book['received_ms'] = received_ms
            book['fix_message_type'] = fields.get('35', header.get('35', ''))
            book['fix_sequence'] = fields.get('34', header.get('34', ''))
            if header.get('35') == 'W' and key not in reset:
                book.update(bid=None, ask=None, bid_size=None, ask_size=None, entries={})
                reset.add(key)
            kind = entry.get('269')
            action = entry.get('279', '0')
            entry_id = entry.get('278') or (kind + ':' + entry['290'] if kind and entry.get('290') else kind)
            old = book['entries'].get(entry.get('280', entry_id), {})
            kind = kind or old.get('269')
            if action == '0':
                old = {}
            if kind == 'J':
                book.update(bid=None, ask=None, bid_size=None, ask_size=None, entries={})
            elif kind in ('0', '1', '2', 'x'):
                side = {'0': 'bid', '1': 'ask', '2': 'last', 'x': 'last'}[kind]
                # TT positions in changes/deletes refer to the book BEFORE this
                # entire message. New levels must not overwrite those positions.
                if kind in ('0', '1') and not entry.get('278') and entry.get('290') and action == '0':
                    entry_id = 'new:' + str(group_index)
                if action == '2':
                    book['entries'].pop(entry_id, None)
                    if kind in ('2', 'x'):
                        book[side] = None
                        book[side + '_size'] = None
                else:
                    merged = dict(old, **entry)
                    if entry.get('280') and entry['280'] != entry_id:
                        book['entries'].pop(entry['280'], None)
                    merged['269'] = kind
                    book['entries'][entry_id] = merged
                    for tag, attribute in [('270', side), ('271', side + '_size')]:
                        if tag in merged:
                            try:
                                value = number(merged[tag], attribute)
                                # Prices in the trader's units; sizes as sent.
                                book[attribute] = (self.to_display(key, value)
                                                   if tag == '270' else float(value))
                            except ValueError:
                                book[attribute] = None
            book['timestamp'] = now()
            book['error'] = ''
        for key in touched:
            book = self.books[key]
            # Only renumber positional entries once all actions have been applied.
            entries = book['entries']
            positional = {k: v for k, v in entries.items() if not v.get('278') and v.get('290')}
            for entry_id in positional:
                entries.pop(entry_id)
            for kind in ('0', '1'):
                values = []
                for value in positional.values():
                    if value.get('269') != kind or not value.get('270'):
                        continue
                    try:
                        number(value['270'], 'price')
                    except ValueError:
                        continue
                    values.append(value)
                values.sort(key=lambda v: Decimal(v['270']), reverse=kind == '0')
                for position, value in enumerate(values, 1):
                    entries[kind + ':' + str(position)] = dict(value, **{'290': str(position)})
            for kind, side in [('0', 'bid'), ('1', 'ask')]:
                levels = []
                for stored in book['entries'].values():
                    if stored.get('269') == kind and stored.get('270') is not None:
                        try:
                            levels.append({'price': self.to_display(key, number(stored['270'], 'price')),
                                           'size': float(number(stored['271'], 'size')) if stored.get('271') else None})
                        except ValueError:
                            continue
                levels.sort(key=lambda level: level['price'], reverse=kind == '0')
                book[side + '_levels'] = levels[:100]
                book[side] = levels[0]['price'] if levels else None
                book[side + '_size'] = levels[0]['size'] if levels else None
            if (book.get('bid') is not None and book.get('ask') is not None
                    and book['bid'] > book['ask']):
                book['error'] = 'Crossed book rejected; waiting for a clean TT snapshot'
                book['stale'] = True
                book['integrity_ok'] = False
            else:
                book['integrity_ok'] = True
            callback = getattr(self.gateway, 'on_market_book', None)
            if callback is not None:
                callback(key, book)

    def _validate(self, args, risk_reducing=False, exclude_order_id=None):
        self.session('Order Routing')
        instrument = self.watch.get(str(args.get('security_id', '')))
        if instrument is None:
            raise ValueError('Add an instrument to the watchlist first')
        if not risk_reducing and self.price_factor(str(args.get('security_id', ''))) is None:
            # Until TT's definition says the display factor, a price typed as
            # the trader knows it (4216.3) would go out as a FIX price (421.63
            # in trader terms) — a sell far through the market. A new order
            # waits a moment; a close never does (it is priced from TT's own
            # book, in TT's own units, both ends alike).
            raise ValueError("TT has not sent this contract's definition yet — its "
                             "prices may still be in TT's units; try again in a moment")
        ticket = {k: clean(args.get(k, '')) for k in ('side', 'order_type', 'tif', 'account', 'text', 'expire_date', 'open_close', 'capacity', 'customer_capacity')}
        if ticket['side'] not in ('BUY', 'SELL') or ticket['order_type'] not in ORDER_TYPES or ticket['tif'] not in TIFS:
            raise ValueError('Choose a supported side, order type and time in force')
        ticket['account'] = clean(ticket['account'] or self.gateway.venue.account, 'Account', True)
        ticket['quantity'] = number(args.get('quantity'), 'Quantity', True)
        ticket['instrument'] = copy.deepcopy(instrument)
        ticket['security_id'] = instrument['security_id']
        if instrument.get('min_qty') and Decimal(ticket['quantity']) < Decimal(number(instrument['min_qty'], 'Minimum quantity', True)):
            raise ValueError('Quantity is below the instrument minimum')
        for key in ('price', 'stop_price', 'min_qty', 'display_qty'):
            value = args.get(key)
            ticket[key] = number(value, key, key in ('min_qty', 'display_qty')) if value not in ('', None) else None
        if ticket['order_type'] in LIMIT_TYPES and ticket['price'] is None:
            raise ValueError('Limit price is required for this order type')
        if ticket['order_type'] in ('STOP', 'STOP_LIMIT') and ticket['stop_price'] is None:
            raise ValueError('Stop trigger price is required')
        if ticket['order_type'] not in LIMIT_TYPES:
            ticket['price'] = None
        if ticket['order_type'] not in ('STOP', 'STOP_LIMIT'):
            ticket['stop_price'] = None
        for key in ('min_qty', 'display_qty'):
            if ticket[key] is not None and Decimal(ticket[key]) > Decimal(ticket['quantity']):
                raise ValueError(key + ' cannot exceed total quantity')
        tick = instrument.get('tick_size')
        if instrument.get('parameters', {}).get('16456', '0') != '0':
            raise ValueError('This instrument has a variable tick table; ticket validation is not yet supported for it')
        if tick:
            tick = Decimal(number(tick, 'Instrument tick size', True))
            for key in ('price', 'stop_price'):
                if ticket[key] is not None and Decimal(ticket[key]) % tick != 0:
                    raise ValueError(key + ' must align with the instrument tick size ' + str(tick))
        if ticket['tif'] == 'GTD':
            try:
                expiry = datetime.strptime(ticket['expire_date'].replace('-', ''), '%Y%m%d').date()
                if expiry < datetime.now(timezone.utc).date():
                    raise ValueError()
                ticket['expire_date'] = expiry.strftime('%Y%m%d')
            except ValueError:
                raise ValueError('GTD requires an expiry date today or later') from None
        if ticket['open_close'] not in ('', 'O', 'C', 'F'):
            raise ValueError('Open/close must be O, C or F')
        if ticket['capacity'] not in ('', 'A', 'G', 'I', 'P', 'R', 'W') or ticket['customer_capacity'] not in ('', '1', '2', '3', '4'):
            raise ValueError('Invalid order capacity')
        ticket['cancel_on_disconnect'] = args.get('cancel_on_disconnect') is True
        if ticket['cancel_on_disconnect'] and ticket['tif'] in ('GTC', 'GTD'):
            raise ValueError('TT cancel-on-disconnect cannot be used with GTC/GTD')
        if ticket['tif'] == 'IOC' and instrument.get('exchange') == 'CME' and ticket['min_qty']:
            raise ValueError('CME IOC must omit minimum quantity; use FOK instead')
        self._risk_check(ticket, risk_reducing, exclude_order_id)
        return ticket

    def _order_fields(self, ticket, client_id):
        fields = [('11', client_id), ('1', ticket['account'])] + self.instrument_fields(ticket['instrument'])
        fields += [('54', '1' if ticket['side'] == 'BUY' else '2'), ('38', ticket['quantity']),
                   ('40', ORDER_TYPES[ticket['order_type']]), ('59', TIFS[ticket['tif']]), ('60', fix_time()), ('1028', 'Y')]
        for key, tag in [('price', '44'), ('stop_price', '99'), ('min_qty', '110'), ('display_qty', '1138'),
                         ('open_close', '77'), ('capacity', '528'), ('customer_capacity', '582'), ('text', '58')]:
            if ticket.get(key) not in (None, ''):
                # The trader's price goes to TT in TT's FIX units.
                value = (self.to_fix(ticket['security_id'], ticket[key])
                         if key in ('price', 'stop_price') else ticket[key])
                fields.append((tag, value))
        if ticket['tif'] == 'GTD':
            fields.append(('432', ticket['expire_date']))
        if ticket['tif'] == 'FOK' and ticket['instrument'].get('exchange') == 'CME':
            fields = [(tag, value) for tag, value in fields if tag not in ('59', '110')]
            fields += [('59', '3'), ('110', ticket['quantity'])]
        if ticket['cancel_on_disconnect']:
            fields.append(('18', 'o 2'))
        return fields

    def _marketable(self, ticket):
        """A MARKET ticket as an exchange takes it: a LIMIT N ticks THROUGH
        the touch, immediate-or-cancel — it fills now or not at all, like a
        market order, at a price the exchange's band accepts. CME via TT
        prices a bare market order itself and REJECTS it outside the band;
        on a close, that is a position that stays open. With no fresh quote
        or no tick it stays a true market order: a close is never withheld
        for want of a price."""
        if ticket.get('order_type') != 'MARKET':
            return ticket
        try:
            ticks = Decimal(str(self.market_ticks(ticket['security_id']) or 0))
            tick = Decimal(str(ticket['instrument'].get('tick_size') or 0))
        except (InvalidOperation, ValueError, TypeError):
            return ticket
        touch = self._touch(ticket)['price']
        if ticks <= 0 or tick <= 0 or touch is None:
            return ticket
        buy = ticket['side'] == 'BUY'
        raw = Decimal(str(touch)) + (ticks * tick if buy else -ticks * tick)
        steps = (raw / tick).to_integral_value(rounding=ROUND_CEILING if buy else ROUND_FLOOR)
        price = (steps * tick).normalize()
        ticket['order_type'] = 'LIMIT'
        ticket['price'] = format(price, 'f')
        ticket['tif'] = 'IOC'
        if ticket['instrument'].get('exchange') == 'CME':
            # TT: a CME IOC that carries a MinQty (110) is treated as a FILL OR
            # KILL — "at market" would quietly become all-or-nothing.
            ticket['min_qty'] = None
        ticket['market_as_limit'] = {'ticks': float(ticks), 'touch': touch}
        return ticket

    def _preview(self, args, risk_reducing=False):
        with self.lock:
            ticket = self._marketable(self._validate(args, risk_reducing))
            token = uuid.uuid4().hex
            self.previews = {k: v for k, v in self.previews.items() if v['expires'] > time.time()}
            self.previews[token] = {'ticket': ticket, 'expires': time.time() + 60}
            return {'ok': True, 'token': token, 'ticket': ticket,
                    'fields': self._order_fields(ticket, 'assigned-on-confirmation'), 'expires_in': 60,
                    'book': self.book_check(ticket)}

    def book_check(self, ticket):
        """The spread a ticket is reviewed against, in words — and WIDE when
        it is over the contract's "Book no wider than": an order at market
        crosses it, and on a thin book that is the whole cost of the trade
        (UAT gold 4218.0 / 4230.0: $1,200 a lot, round trip). Shown, never a
        refusal: the trader decides."""
        sid = ticket.get('security_id')
        book = self.books.get(sid) or {}
        bid, ask = book.get('bid'), book.get('ask')
        out = {'bid': bid, 'ask': ask, 'spread': None, 'ticks': None, 'money': None,
               'limit_ticks': None, 'wide': False, 'text': None}
        if bid is None or ask is None:
            out['text'] = 'no bid and offer from TT right now — the spread is not known'
            return out
        instrument = ticket.get('instrument') or self.watch.get(sid) or {}
        live = self.watch.get(sid) or {}
        try:
            tick = float(live.get('tick_size') or instrument.get('tick_size') or 0)
            value = float(live.get('tick_value') or instrument.get('tick_value') or 0)
        except (TypeError, ValueError):
            tick = value = 0.0
        spread = float(ask) - float(bid)
        out['spread'] = round(spread, 10)
        if tick > 0:
            out['ticks'] = round(spread / tick, 4)
            if value > 0:
                out['money'] = round(out['ticks'] * value * float(ticket.get('quantity') or 0), 2)
        try:
            limit = float(self.spread_limit(sid) or 0)
        except (TypeError, ValueError):
            limit = 0.0
        out['limit_ticks'] = limit or None
        cost = f" — about {out['money']:,.2f} to cross, round trip" if out['money'] is not None else ''
        size = f"{spread:g} wide" + (f" ({out['ticks']:g} ticks)" if out['ticks'] is not None else '')
        out['wide'] = bool(limit and out['ticks'] is not None and out['ticks'] > limit + 1e-9)
        out['text'] = (f"Spread {bid:g} / {ask:g}: {size}{cost}"
                       + (f". WIDER than this contract's {limit:g}-tick limit." if out['wide'] else ''))
        return out

    def _refuse_if_blocked(self, ticket=None):
        reason = self.mode_block(ticket)
        if reason:
            raise ValueError(reason)

    def preview(self, args):
        self._refuse_if_blocked({'security_id': str(args.get('security_id', ''))})
        return self._preview(args)

    def open_business(self, security_id=None):
        """What the manual side has open or working, in words.

        A working order, or filled quantity not yet closed. UNKNOWN (a
        restart that could not recover a venue status) is not counted as
        open — it would block a switch for ever — but it is named, because
        unknown is not the same as gone."""
        with self.lock:
            out = []
            for order in self.orders.values():
                if security_id and str(order['ticket'].get('security_id')) != str(security_id):
                    continue
                name = order['ticket']['instrument'].get('display_name') or order['ticket'].get('security_id')
                if order['status'] in ('PENDING', 'NEW', 'PARTIALLY_FILLED', 'REPLACED'):
                    out.append(f"{name}: working manual order {order['id']}")
                elif self.closeable(order) > 0:
                    out.append(f"{name}: {self.closeable(order)} filled on {order['id']} not yet closed")
            return out

    def close_alerts(self):
        """Manual positions whose LAST close did not happen — TT refused it,
        or a fill-or-cancel close found nothing — and that are still open.
        Said on the ladder and the Trading Monitor, in TT's words, until a
        later close fills or the position is flat."""
        with self.lock:
            out = []
            for source in self.orders.values():
                open_qty = self.closeable(source)
                if open_qty <= 0:
                    continue
                children = [c for c in self.orders.values() if c.get('close_of') == source['id']]
                if not children:
                    continue
                last = max(children, key=lambda c: c.get('updated') or '')
                ticket = source['ticket']
                name = ticket['instrument'].get('display_name') or ticket.get('security_id')
                held = f"still {'long' if ticket['side'] == 'BUY' else 'short'} {open_qty:g} {name}"
                if last['status'] == 'REJECTED':
                    text = f"Your close was REJECTED — {held}. TT: {last.get('text') or 'no reason given'}"
                elif (last['status'] in ('CANCELED', 'EXPIRED') and not last.get('filled_qty')
                      and last['ticket'].get('tif') == 'IOC'):
                    text = (f"Your close did not fill — the price moved before it got there; "
                            f"{held}. Press CLOSE ALL again, or Close @ LMT.")
                else:
                    continue
                out.append({'security_id': str(ticket.get('security_id')),
                            'order_id': source['id'], 'close_id': last['id'],
                            'text': text, 'at': last.get('updated')})
            return out

    #: How long TT may take to answer a price request before the screen
    #: says it has not.
    FEED_ANSWER_SEC = 10

    def feed_status(self, key):
        """Why this instrument has prices or not, in words a trader can act
        on: TT refused the request (permissions — Orient / TT), TT answered
        but nobody quotes it (a quiet UAT market), TT has not answered (a
        Security ID it does not know), or prices are arriving."""
        with self.lock:
            key = str(key or '')
            md = self.gateway._sessions.get('Market Data')
            if not key:
                return {'state': 'NO_ID', 'text': 'no TT Security ID on this contract'}
            if md is None or md.state.status != 'CONNECTED':
                return {'state': 'NO_SESSION', 'text': 'Market Data is not connected to TT'}
            if key not in self.watch:
                return {'state': 'NOT_REQUESTED', 'text': 'not on the Market Data watchlist yet'}
            book = self.books.get(key)
            if book is None or key not in self.subscriptions:
                return {'state': 'NOT_REQUESTED', 'text': 'prices not requested from TT yet'}
            if book.get('error'):
                return {'state': 'REFUSED', 'text': 'TT refused the price request: ' + book['error'] +
                        ' — usually market-data permission for this product on the account '
                        '(ask Orient / TT to enable it)'}
            waited = time.time() - float(book.get('requested_at') or time.time())
            if not book.get('answered'):
                if waited < self.FEED_ANSWER_SEC:
                    return {'state': 'WAITING', 'text': 'prices requested from TT — waiting for the first answer'}
                return {'state': 'NO_ANSWER', 'text': f'TT has not answered the price request in {waited:.0f}s '
                        f'(Security ID {key}) — check the Security ID is right and the contract still '
                        'listed; if it is, ask Orient / TT whether this product has market data on UAT'}
            bid, ask = book.get('bid'), book.get('ask')
            if bid is None and ask is None:
                return {'state': 'EMPTY', 'text': 'TT answered, but nobody is quoting it — no bid and no '
                        'offer on this market right now (common on UAT)'}
            if bid is None or ask is None:
                return {'state': 'ONE_SIDED', 'text': 'TT is sending only ' +
                        ('an offer' if bid is None else 'a bid') + ' — nobody on the other side'}
            return {'state': 'LIVE', 'text': 'prices arriving from TT'}

    def closeable(self, order):
        """Unclosed fills in this ticket's ledger, with outstanding closes reserved."""
        if order.get('close_of') or order['ticket'].get('open_close') in ('C', 'F'):
            return Decimal('0')
        if order['status'] not in TERMINAL or order.get('pending'):
            return Decimal('0')
        remaining = Decimal(str(order.get('filled_qty', 0)))
        for child in self.orders.values():
            if child.get('close_of') == order['id']:
                # UNKNOWN is reserved too: a lost acknowledgement is not a rejection.
                remaining -= (Decimal(str(child['filled_qty'])) if child['status'] in TERMINAL
                              else Decimal(child['ticket']['quantity']))
        return max(Decimal('0'), remaining)

    def preview_close(self, args):
        with self.lock:
            source = self.orders.get(str(args.get('order_id', '')))
            if source is None or self.closeable(source) <= 0:
                raise ValueError('No unclosed filled quantity is available. Cancel any working remainder first.')
            original = source['ticket']
            # Use the immutable original instrument, even if removed from the watchlist.
            instrument = original['instrument']
            key = original['security_id']
            previous = self.watch.get(key)
            self.watch[key] = instrument
            try:
                result = self._preview({'security_id': key, 'account': original['account'],
                    'side': 'SELL' if original['side'] == 'BUY' else 'BUY',
                    # A price makes it a resting LIMIT close (the ladder's
                    # Close @ LMT); without one it crosses at market.
                    'order_type': 'LIMIT' if args.get('price') not in (None, '') else 'MARKET',
                    'price': args.get('price') if args.get('price') not in (None, '') else '',
                    'quantity': str(self.closeable(source)),
                    'tif': 'DAY', 'open_close': 'C', 'capacity': original.get('capacity', ''),
                    'customer_capacity': original.get('customer_capacity', ''),
                    'text': 'Close ' + source['id']}, risk_reducing=True)
            finally:
                if previous is None:
                    self.watch.pop(key, None)
                else:
                    self.watch[key] = previous
            self.previews[result['token']]['close_of'] = source['id']
            result['close_of'] = source['id']
            return result

    def submit(self, args):
        with self.lock:
            token = str(args.get('token', ''))
            # TT: ClOrdID (11) is at most 20 characters — FTM- and 16 hex.
            order_id = 'FTM-' + token[:16]
            if order_id in self.orders:
                return {'ok': True, 'order_id': order_id, 'status': self.orders[order_id]['status']}
            preview = self.previews.get(token)
            if not preview or preview['expires'] < time.time() or args.get('confirmed') is not True:
                raise ValueError('Review the ticket again and confirm before sending')
            self.session('Order Routing')
            ticket = copy.deepcopy(preview['ticket'])
            close_of = preview.get('close_of')
            if not close_of:
                # A ticket reviewed before its Algo went to trading is
                # refused at the send, not just at the review.
                self._refuse_if_blocked(ticket)
            self._risk_check(ticket, risk_reducing=bool(close_of))
            if close_of and Decimal(ticket['quantity']) > self.closeable(self.orders[close_of]):
                raise ValueError('The available close quantity changed. Review the close again.')
            order = {'id': order_id, 'current_id': order_id, 'ids': [order_id], 'ticket': ticket,
                     'status': 'PENDING', 'text': 'Awaiting TT acknowledgement', 'updated': now(),
                     'filled_qty': 0, 'remaining_qty': float(ticket['quantity']), 'avg_price': None, 'venue_order_id': '', 'pending': None}
            # Prices in trader units once TT's factor is known; an order sent
            # before it was is brought over by `_migrate_units` when it is.
            if self.price_factor(ticket['security_id']) is not None:
                order['units'] = 'display'
            if close_of:
                order['close_of'] = close_of
            # The price the trader could have crossed at when the ticket was
            # SENT — the anchor this order's slippage is measured against.
            order['decision'] = self._touch(ticket)
            self.orders[order_id] = order
            self._save('order', order_id, order)  # Durable idempotency before any bytes go out.
            del self.previews[token]
            try:
                self._send('Order Routing', 'D', self._order_fields(ticket, order_id))
            except Exception:
                order['status'] = 'UNKNOWN'
                order['text'] = 'Send outcome unknown. Verify in TT; this order will not be retried.'
                self._save('order', order_id, order)
                raise
            return {'ok': True, 'order_id': order_id, 'status': order['status']}

    def manage(self, args, replace=False):
        with self.lock:
            order = self.orders.get(str(args.get('order_id')))
            if not order or not order['id'].startswith('FTM-'):
                raise ValueError('Only orders created by this manual ticket can be managed here')
            if order['status'] not in ('NEW', 'PARTIALLY_FILLED', 'REPLACED') or order.get('pending'):
                raise ValueError('Order must be acknowledged and have no pending change')
            self.session('Order Routing')
            new_id = 'FTM-' + uuid.uuid4().hex[:16]    # TT: 20 characters at most
            ticket = copy.deepcopy(order['ticket'])
            if replace:
                self._refuse_if_blocked(ticket)
                if order.get('close_of'):
                    raise ValueError('Close orders cannot be increased or replaced here; cancel and review the remaining close quantity.')
                for key in ('quantity', 'price', 'stop_price'):
                    if key in args:
                        ticket[key] = args[key]
                ticket = self._validate(ticket, exclude_order_id=order['id'])
                if Decimal(ticket['quantity']) <= Decimal(str(order['filled_qty'])):
                    raise ValueError('Replacement total quantity must exceed quantity already filled')
                fields = self._order_fields(ticket, new_id)
            else:
                fields = [('11', new_id), ('1', ticket['account']), ('54', '1' if ticket['side'] == 'BUY' else '2'),
                          ('38', ticket['quantity']), ('60', fix_time()), ('1028', 'Y')] + self.instrument_fields(ticket['instrument'])
            fields += [('41', order['current_id'])]
            if order['venue_order_id']:
                fields.append(('37', order['venue_order_id']))
            order['ids'].append(new_id)
            order['pending'] = {'id': new_id, 'kind': 'REPLACE' if replace else 'CANCEL', 'ticket': ticket}
            order['text'] = 'Change requested; awaiting TT acknowledgement'
            self._save('order', order['id'], order)
            try:
                self._send('Order Routing', 'G' if replace else 'F', fields)
            except Exception:
                order['status'] = 'UNKNOWN'
                order['text'] = 'Change outcome unknown; verify in TT before retrying'
                self._save('order', order['id'], order)
                raise
            return {'ok': True, 'order_id': order['id']}

    def _execution(self, fields):
        order = next((o for o in self.orders.values() if fields.get('11') in o['ids'] or fields.get('41') in o['ids']), None)
        if order is None:
            return
        if fields.get('35') == '9':
            order['pending'] = None
            order['status'] = self._later_status(order['status'], {
                '0': 'NEW', '1': 'PARTIALLY_FILLED', '2': 'FILLED', '4': 'CANCELED',
                'C': 'EXPIRED'}.get(fields.get('39'), 'UNKNOWN'))
            order['text'] = self.gateway._redact(fields.get('58', 'TT rejected the order change'))
        else:
            from . import tt_exec
            what = tt_exec.kind(fields)
            if what in (tt_exec.BUST, tt_exec.CORRECTION):
                # A change to an earlier fill: said, never booked as new.
                text = tt_exec.correction_text(fields, what)
                self.errors = (self.errors + [text])[-10:]
                order['text'] = text
                order['updated'] = now()
                self._save('order', order['id'], order)
                return
            if what == tt_exec.LEG:
                return          # a spread leg's own fill: the spread is booked once
            key = tt_exec.exec_key(fields)
            if what == tt_exec.FILL and self.db.execute('SELECT 1 FROM manual_fills WHERE id=?', (key,)).fetchone():
                return
            states = {'0':'NEW', '1':'PARTIALLY_FILLED', '2':'FILLED', '4':'CANCELED', '5':'REPLACED',
                      '6':'PENDING_CANCEL', '8':'REJECTED', 'A':'PENDING', 'C':'EXPIRED', 'E':'PENDING_REPLACE'}
            reported = states.get(fields.get('39'), 'UNKNOWN')
            stale = order['status'] in TERMINAL and reported not in TERMINAL
            order['status'] = self._later_status(order['status'], reported)
            order['venue_order_id'] = fields.get('37', order['venue_order_id'])
            pending = order.get('pending')
            if pending and fields.get('11') == pending['id'] and fields.get('150') == '5':
                order['ticket'] = pending['ticket']
                order['current_id'] = pending['id']
                order['pending'] = None
            if order['status'] in TERMINAL:
                order['pending'] = None
            sid = order['ticket'].get('security_id')
            for tag, key in [('14', 'filled_qty'), ('151', 'remaining_qty'), ('6', 'avg_price')]:
                if fields.get(tag) is not None and not stale:
                    order[key] = (self.to_display(sid, number(fields[tag], key))
                                  if tag == '6' else float(number(fields[tag], key)))
            if not stale:
                order['text'] = self.gateway._redact(fields.get('58', ''))
            if order['status'] == 'REJECTED':
                order['text'] = tt_exec.reject_reason(fields, order['text'])
            if fields.get('17') and what == tt_exec.FILL:
                fill = {'exec_id': fields['17'], 'order_id': order['id'], 'symbol': order['ticket']['instrument']['description'],
                        'side': order['ticket']['side'], 'quantity': fields.get('32'),
                        'price': (self.to_display(sid, fields['31']) if fields.get('31') else None), 'time': now(),
                        'order_type': order['ticket']['order_type'], 'close': bool(order.get('close_of'))}
                fill.update(self._fill_slippage(order, fill['price'], fields.get('32')))
                if self.price_factor(sid) is not None:
                    fill['units'] = 'display'
                self.db.execute('INSERT OR IGNORE INTO manual_fills VALUES (?,?)', (key, json.dumps(fill)))
        order['updated'] = now()
        self._save('order', order['id'], order)

    @staticmethod
    def _later_status(current, new):
        """An order is never moved back from a final state: a report
        replayed by TT FIX Recovery (or resent) is older than the FILLED or
        CANCELED already applied."""
        if current in TERMINAL and new not in TERMINAL:
            return current
        return new

    def _touch(self, ticket):
        """The book when a ticket is sent: the side it would cross — the
        offer for a BUY, the bid for a SELL — and both sides beside it. A
        missing or stale quote leaves the price None: that order's slippage
        is then unmeasured, never zero."""
        book = self.books.get(ticket['security_id']) or {}
        stamp = book.get('timestamp')
        try:
            age = ((datetime.now(timezone.utc) - datetime.fromisoformat(stamp))
                   .total_seconds() if stamp else None)
        except (TypeError, ValueError):
            age = None
        fresh = age is not None and age <= 15 and book.get('integrity_ok') is not False
        price = book.get('ask') if ticket['side'] == 'BUY' else book.get('bid')
        return {'price': price if fresh else None, 'bid': book.get('bid'),
                'ask': book.get('ask'), 'at': now(),
                'why_not': None if fresh and price is not None else
                ('no fresh TT quote when the ticket was sent')}

    @staticmethod
    def _slip_values(instrument, side, expected, filled, quantity):
        """(points, ticks, money) of a fill against `expected`, positive a
        cost; each None where it cannot be priced."""
        try:
            points = slippage_mod.slip(side, None if expected is None else float(expected),
                                       None if filled is None else float(filled))
        except (TypeError, ValueError):
            points = None
        if points is None:
            return None, None, None
        try:
            tick = float(instrument.get('tick_size') or 0)
        except (TypeError, ValueError):
            tick = 0.0
        ticks = round(points / tick, 4) if tick > 0 else None
        try:
            value = float(instrument.get('tick_value') or 0)
            money = (ticks * value * float(quantity)
                     if ticks is not None and value > 0 and quantity not in (None, '') else None)
        except (TypeError, ValueError):
            money = None
        return points, ticks, money

    def _fill_slippage(self, order, price, quantity):
        decision = order.get('decision') or {}
        points, ticks, money = self._slip_values(
            order['ticket']['instrument'], order['ticket']['side'],
            decision.get('price'), price, quantity)
        return {'decision_price': decision.get('price'),
                'slippage_points': points, 'slippage_ticks': ticks,
                'slippage_money': money}

    def slippage_rows(self):
        """One row per manual ticket with fills: its average fill against
        the touch when it was sent, positive a cost. ENTRY for an opening
        ticket, EXIT for a close. Rows recorded before the touch was kept are
        unmeasured, never zero."""
        rows = []
        for order in self.orders.values():
            if not order.get('filled_qty') or order.get('avg_price') is None:
                continue
            ticket = order['ticket']
            decision = order.get('decision') or {}
            points, ticks, money = self._slip_values(
                ticket['instrument'], ticket['side'], decision.get('price'),
                order['avg_price'], order['filled_qty'])
            rows.append({'order_id': order['id'], 'end': 'exit' if order.get('close_of') else 'entry',
                         'instrument': ticket['instrument'].get('display_name') or ticket['instrument'].get('description'),
                         'security_id': ticket['security_id'], 'side': ticket['side'],
                         'order_type': ticket['order_type'], 'quantity': float(order['filled_qty']),
                         'decision_price': decision.get('price'), 'avg_price': order['avg_price'],
                         'slippage_points': points, 'slippage_ticks': ticks, 'slippage_money': money,
                         'why_not': (None if ticks is not None else decision.get('why_not')
                                     or 'sent before the touch was recorded'),
                         'time': order.get('updated')})
        return rows

    @staticmethod
    def _money_pnl(instrument, entry, exit_price, quantity, side):
        """PnL using only TT definition values; unknown metadata stays unknown."""
        try:
            tick = Decimal(str(instrument.get('tick_size') or ''))
            tick_value = Decimal(str(instrument.get('tick_value') or ''))
            entry = Decimal(str(entry)); exit_price = Decimal(str(exit_price))
            quantity = Decimal(str(quantity))
            if tick <= 0:
                return None
            direction = Decimal('1') if side == 'BUY' else Decimal('-1')
            return float((exit_price - entry) / tick * tick_value * quantity * direction)
        except (InvalidOperation, ValueError, TypeError, ZeroDivisionError):
            return None

    def _pnl_snapshot(self, watch_rows):
        quotes = {row['instrument']['security_id']: row['quote'] for row in watch_rows}
        trades, positions = [], []
        realized_total = Decimal('0'); floating_total = Decimal('0')
        realized_complete = floating_complete = True
        for order in self.orders.values():
            if order.get('close_of') or not order.get('filled_qty') or order.get('avg_price') is None:
                continue
            instrument = order['ticket']['instrument']; security_id = order['ticket']['security_id']
            closed = Decimal('0')
            for close in self.orders.values():
                if close.get('close_of') != order['id'] or not close.get('filled_qty'):
                    continue
                qty = Decimal(str(close['filled_qty'])); closed += qty
                value = self._money_pnl(instrument, order['avg_price'], close.get('avg_price'),
                                        qty, order['ticket']['side'])
                if value is None:
                    realized_complete = False
                else:
                    realized_total += Decimal(str(value))
                trades.append({'entry_order_id': order['id'], 'exit_order_id': close['id'],
                    'instrument': instrument.get('display_name') or instrument.get('description'),
                    'security_id': security_id, 'side': order['ticket']['side'],
                    'quantity': float(qty), 'entry_price': order['avg_price'],
                    'exit_price': close.get('avg_price'), 'realized_pnl': value,
                    'currency': instrument.get('currency') or None,
                    'closed_at': close.get('updated'), 'source': 'TT execution reports'})
            open_qty = max(Decimal('0'), Decimal(str(order['filled_qty'])) - closed)
            if open_qty:
                quote = quotes.get(security_id, {}); mark = quote.get('mid')
                fresh = bool(mark is not None and not quote.get('stale'))
                value = self._money_pnl(instrument, order['avg_price'], mark, open_qty,
                                        order['ticket']['side']) if fresh else None
                if value is None:
                    floating_complete = False
                else:
                    floating_total += Decimal(str(value))
                positions.append({'entry_order_id': order['id'],
                    'instrument': instrument.get('display_name') or instrument.get('description'),
                    'security_id': security_id, 'side': order['ticket']['side'],
                    'quantity': float(open_qty), 'entry_price': order['avg_price'],
                    'mark_price': mark if fresh else None, 'floating_pnl': value,
                    'currency': instrument.get('currency') or None,
                    'mark_source': 'TT FIX bid/ask midpoint' if fresh else 'Unavailable: TT quote is stale or missing',
                    'quote_sequence': quote.get('fix_sequence'), 'quote_time': quote.get('timestamp')})
        currencies = {row.get('currency') for row in trades + positions if row.get('currency')}
        currency = next(iter(currencies)) if len(currencies) == 1 else None
        return {'trades': trades[::-1], 'positions': positions,
                'realized_total': float(realized_total) if realized_complete else None,
                'floating_total': float(floating_total) if floating_complete else None,
                'currency': currency, 'fees_included': False,
                'account': {'name': self.gateway.venue.account,
                    'balance': None, 'equity': None, 'margin': None,
                    'status': ('Unavailable from this TT FIX Order Routing / Market Data session. '
                               'Tag 1 identifies the order account but is not an account-balance feed.')}}

    def snapshot(self):
        with self.lock:
            rows = []
            md = self.gateway._sessions.get('Market Data')
            for key, instrument in self.watch.items():
                book = {k: v for k, v in self.books.get(key, {}).items() if k != 'entries'}
                stamp = book.get('timestamp')
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(stamp)).total_seconds() if stamp else None
                book['stale'] = (book.get('integrity_ok') is False or not md
                                 or md.state.status != 'CONNECTED' or age is None or age > 15)
                bid, ask = book.get('bid'), book.get('ask')
                book['spread'] = ask - bid if bid is not None and ask is not None else None
                book['mid'] = (ask + bid) / 2 if bid is not None and ask is not None else None
                book['feed_status'] = self.feed_status(key)
                rows.append({'instrument': instrument, 'quote': book})
            pnl = self._pnl_snapshot(rows)
            return copy.deepcopy({'search': self.search, 'instruments': list(self.instruments.values())[:2000],
                'catalogue': [{k: i.get(k, '') for k in CATALOGUE_FIELDS}
                              for i in self.catalogue.values()],
                'watchlist': rows, 'orders': [dict(o, close_available=float(self.closeable(o)),
                    closed_qty=sum(float(c['filled_qty']) for c in self.orders.values() if c.get('close_of') == o['id']))
                    for o in list(self.orders.values())[-200:][::-1]],
                'fills': [json.loads(row[0]) for row in self.db.execute('SELECT data FROM manual_fills ORDER BY rowid DESC LIMIT 100')],
                'errors': self.errors, 'account': self.gateway.venue.account, 'pnl': pnl,
                'close_alerts': self.close_alerts(),
                'slippage': slippage_mod.manual_summary(self.slippage_rows()),
                'risk': copy.deepcopy(self.risk),
                'order_types': list(ORDER_TYPES), 'tifs': list(TIFS)})
