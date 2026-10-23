"""A small fake Stellar, spoken to over the REAL FIX sessions.

Both sessions — the Order Router and Market Data — connect to it through a
monkeypatched `socket.create_connection`, so everything between the screen
and the wire is the production code. It speaks the dialect of Stellar's two
specifications (FIX 4.4, TargetCompID STELLAR) for the cases a desk relies on:

- Market Data: a Security List Request (x) — a SYMBOL and a CFI code — is
  answered with a Security List (y): Security ID (48), CFI (461), maturity
  (200), tick (969), tick value (1146) and, for a strategy, its legs (555).
  A Market Data Request (V) by Security ID is answered with a snapshot (W)
  whose levels carry MDEntryPositionNo (290); every `move()` publishes an
  Incremental Refresh (X) of position 1 each side. Stellar's prices ARE
  trader prices: 90.50 is sent as 90.50.
- New Order Single (D) for an outright, New Order Multi-leg (AB) for a
  strategy — the instrument named by symbol, CFI, maturity / legs, never by
  Security ID; the account in the Parties group (452=24). Acknowledged
  (150=0, ExecID "ORD:n"); a MARKET or a crossing LIMIT fills at the touch,
  otherwise it RESTS and fills at its own price when the market reaches it.
  A trade (150=F) carries NO ClOrdID — only Stellar's OrderID (37) — with
  AvgPx 0 and CumQty 0, as the spec's examples show; a strategy's trade comes
  as the strategy (442=3) AND one report per leg (442=2).
- Cancel (F) and Cancel/Replace (G / AC) of a resting order by OrderID (37);
  one it does not know is a Cancel Reject (9) in its own words.
- Order Status Request (H): an Execution Report 150=I carrying the 790.
- An unknown account is rejected (150=8, 103=99) with tag 58.
- The Order Router keeps its sequence numbers per SenderCompID across
  connections and honours a ResendRequest (2) with the messages themselves,
  43=Y. `drop_reports` > 0 loses the next N reports on the way — numbered,
  kept, never delivered — as a line that was down; the next message the
  program receives shows the gap. Market Data starts at 1 on every logon.

It is a stand-in for UAT in the cloud, not a model of Stellar's matching.
"""
import socket
import threading
import time

from fixtrader.gateway import encode_fix_message, parse_fix_message

SOH = '\x01'


def pairs(raw):
    return [tuple(p.split('=', 1)) for p in raw.split(SOH) if '=' in p]


def party(raw, role):
    pending = None
    for tag, value in pairs(raw):
        if tag == '448':
            pending = value
        elif tag == '452' and pending is not None:
            if value == role:
                return pending
            pending = None
    return None


def legs_of(raw):
    legs, leg = [], None
    for tag, value in pairs(raw):
        if tag == '600':
            if leg:
                legs.append(leg)
            leg = {'600': value}
        elif leg is not None and tag in ('608', '610', '623', '624'):
            leg[tag] = value
        elif leg is not None and tag not in ('608', '610', '612', '623', '624'):
            legs.append(leg)
            leg = None
    if leg:
        legs.append(leg)
    return legs


class Exchange:
    def __init__(self, accounts=('ACC1',)):
        self.accounts = set(accounts)
        self.books = {}            # sid -> instrument and its book
        self.resting = {}          # OrderID -> order dict
        self.orders = {}           # OrderID -> order dict (every order)
        self.positions = {}        # (account, sid) -> net qty
        self.peers = []
        self.lock = threading.RLock()
        self._exec = 0
        self._oid = 0
        self._trade = 0
        #: Every order message the program SENT, as tag -> value (the first
        #: of each tag) with its raw text, for a test to read back.
        self.orders_in = []
        #: When set, every new order is REJECTED with these words.
        self.reject_text = None
        #: The book is SHOWN but nothing at it trades — a thin UAT market:
        #: an immediate-or-cancel order is cancelled unfilled.
        self.no_liquidity = False
        #: The Order Router's own numbering, per SenderCompID, across
        #: connections: {'seq': last sent, 'sent': {seq: fields}}.
        self.or_sessions = {}
        #: Execution Reports lost on the way (numbered, never delivered).
        self.drop_reports = 0
        #: Execution Reports never sent at all (not even numbered): an
        #: acknowledgement whose OrderID the program never hears.
        self.swallow_reports = 0
        #: Order Status Requests (H) answered — or, when silent, not.
        self.status_requests = []
        self.status_silent = False
        #: Mass Order Status Requests (AF) received.
        self.mass_requests = []
        #: Logouts received ((session kind, text)); Stellar answers each one
        #: with its own Logout unless silent.
        self.logouts = []
        self.logout_silent = False
        #: ResendRequests this fake sent for numbers we skipped, and the
        #: GapFills that answered them ((comp, seq, new seq)).
        self.resend_requests_sent = []
        self.gap_fills = []
        #: Security List Requests (x) received.
        self.list_requests = []

    # -- the market ----------------------------------------------------------

    def list(self, sid, bid, ask, tick=0.01, size=50, symbol='CL', exchange='XCME',
             tick_value=10.0, cfi='FXXXXX', maturity='202612', legs=None,
             subtype='', leg_prices=None):
        """An instrument as Stellar lists it: an outright future (F) by its
        maturity, or a strategy (M) by its legs ({'600','610','623','624'})."""
        self.books[sid] = {'bid': bid, 'ask': ask, 'bid_size': size,
                           'ask_size': size, 'tick': tick, 'symbol': symbol,
                           'exchange': exchange, 'tick_value': tick_value,
                           'cfi': cfi, 'maturity': maturity, 'legs': legs or [],
                           'subtype': subtype, 'leg_prices': leg_prices or {}}

    def security_list(self, peer, f):
        self.list_requests.append(dict(f))
        want_cfi = (f.get('461') or 'F')[:1]
        found = [(sid, b) for sid, b in self.books.items()
                 if (not f.get('55') or b['symbol'] == f['55'])
                 and (not f.get('461') or b['cfi'][:1] == want_cfi)
                 and (not f.get('207') or b['exchange'] == f['207'])]
        fields = [('35', 'y')]
        if not found:
            peer.reply(fields + [('320', f.get('320', '')), ('322', '1_R'), ('560', '2'),
                                 ('893', 'Y')])
            return
        fields.append(('146', str(len(found))))
        for sid, b in found:
            fields += [('55', b['symbol']), ('15', 'USD'), ('48', sid)]
            if b['cfi'].startswith('F'):
                fields.append(('200', b['maturity']))
            fields += [('207', b['exchange']), ('461', b['cfi']),
                       ('969', repr(b['tick'])), ('1146', repr(b['tick_value']))]
            if b['legs']:
                fields.append(('555', str(len(b['legs']))))
                for leg in b['legs']:
                    fields += [('600', leg['600']), ('608', leg.get('608', 'FXXXXX')),
                               ('610', leg['610']), ('623', leg.get('623', '1')),
                               ('624', leg['624'])]
                if b['subtype']:
                    fields.append(('762', b['subtype']))
        fields += [('320', f.get('320', '')), ('322', '1_R'), ('393', str(len(found))),
                   ('560', '0'), ('893', 'Y')]
        peer.reply(fields)

    def move(self, sid, bid, ask):
        """The market moves: publish it, and fill what it reached."""
        with self.lock:
            book = self.books[sid]
            book['bid'], book['ask'] = bid, ask
            for peer in self.peers:
                peer.publish_change(sid)
            for oid, order in list(self.resting.items()):
                if order['sid'] != sid:
                    continue
                if order['side'] == '1' and order['price'] >= ask:
                    self._fill(order, order['price'])
                elif order['side'] == '2' and order['price'] <= bid:
                    self._fill(order, order['price'])

    # -- helpers ------------------------------------------------------------------

    def next_exec(self):
        self._exec += 1
        return str(13297200 + self._exec)

    def or_peer(self):
        live = [p for p in self.peers if p.kind == 'OR' and not p.closed]
        return live[-1] if live else None

    def _base(self, order):
        fields = [('35', '8'), ('37', order['oid']), ('21', '1'), ('54', order['side']),
                  ('55', order['symbol']), ('207', order['exchange']),
                  ('60', time.strftime('%Y%m%d-%H:%M:%S.000', time.gmtime())),
                  ('461', order['cfi']), ('167', 'MLEG' if order['legs'] else 'FUT')]
        if not order['legs']:
            fields.append(('200', order['maturity']))
        fields += [('453', '2'), ('448', 'USER'), ('447', 'D'), ('452', '11'),
                   ('448', order['account']), ('447', 'D'), ('452', '24')]
        return fields

    def _order_report(self, order, exec_type, status, **extra):
        """An order transaction: ClOrdID, ExecID 'ORD:n', no 41 on a cancel."""
        fields = self._base(order) + [('11', order['report_id']), ('17', 'ORD:' + self.next_exec()),
                                      ('150', exec_type), ('39', status),
                                      ('38', str(order['qty'])), ('40', order['type']),
                                      ('59', order['tif']), ('6', '0'), ('14', '0'),
                                      ('442', '3' if order['legs'] else '1')]
        if order['price'] is not None:
            fields.append(('44', repr(order['price'])))
        for tag, value in extra.items():
            fields.append((tag.lstrip('t'), str(value)))
        self.deliver(fields)

    def deliver(self, fields):
        peer = self.or_peer()
        if peer is None:
            self.or_record(None, fields)              # kept for a resend
            return
        if self.swallow_reports > 0 and fields[0][1] in ('8', '9'):
            self.swallow_reports -= 1                 # never numbered, never sent
            return
        if self.drop_reports > 0 and fields[0][1] in ('8', '9'):
            self.drop_reports -= 1
            self.or_record(peer.comp, fields)         # numbered, never delivered
            return
        peer.reply(fields)

    def or_record(self, comp, fields):
        comp = comp or (self.or_peer().comp if self.or_peer() else None) or self._last_or_comp
        session = self.or_sessions.setdefault(comp, {'seq': 0, 'sent': {}})
        session['seq'] += 1
        session['sent'][session['seq']] = fields
        return session['seq']

    _last_or_comp = None

    def _fill(self, order, price):
        qty = order['qty'] - order['cum']
        order['cum'] += qty
        key = (order['account'], order['sid'])
        sign = 1 if order['side'] == '1' else -1
        self.positions[key] = self.positions.get(key, 0) + sign * qty
        self.resting.pop(order['oid'], None)
        self._trade += 1
        trade = str(12059700 + self._trade)
        # The trade: no ClOrdID, AvgPx "0", CumQty 0 — Stellar's own example.
        fields = self._base(order) + [('17', self.next_exec()), ('19', '0'), ('527', trade),
                                      ('150', 'F'), ('39', '2'), ('32', str(qty)),
                                      ('31', repr(price)), ('14', '0'), ('151', '0'),
                                      ('6', '0'), ('15', 'USD'),
                                      ('75', time.strftime('%Y%m%d', time.gmtime())),
                                      ('442', '3' if order['legs'] else '1')]
        self.deliver(fields)
        for leg in order['legs']:
            # Each leg's own trade, at the leg's own price: never the spread's.
            leg_price = self.books[order['sid']]['leg_prices'].get(leg['610'], 99.0)
            self.deliver([('35', '8'), ('37', order['oid']), ('17', self.next_exec()), ('19', '0'),
                          ('527', trade + '-' + leg['610']), ('150', 'F'), ('39', '2'),
                          ('54', '1' if (leg['624'] == '1') == (order['side'] == '1') else '2'),
                          ('55', leg['600']), ('200', leg['610']), ('32', str(qty)),
                          ('31', repr(leg_price)), ('14', '0'), ('151', '0'), ('6', '0'),
                          ('442', '2'), ('461', 'FXXXXX'), ('167', 'FUT')])

    # -- the order session ----------------------------------------------------------

    def on_order(self, f, raw):
        with self.lock:
            kind = f['35']
            if kind in ('D', 'AB', 'F', 'G', 'AC', 'H', 'AF'):
                self.orders_in.append(dict(f, raw=raw))
            if kind in ('D', 'AB'):
                self._new(f, raw)
            elif kind == 'F':
                self._cancel(f)
            elif kind in ('G', 'AC'):
                self._replace(f)
            elif kind == 'H':
                self._status(f)
            elif kind == 'AF':
                self._mass_status(f)

    def _instrument(self, f, raw):
        legs = legs_of(raw)
        for sid, b in self.books.items():
            if b['symbol'] != f.get('55') or b['cfi'][:1] != (f.get('461') or 'F')[:1]:
                continue
            if b['legs']:
                if [(l['600'], l['610'], l['624']) for l in b['legs']] == \
                        [(l.get('600'), l.get('610'), l.get('624')) for l in legs]:
                    return sid, b
            elif f.get('200') == b['maturity']:
                return sid, b
        return None, None

    def _new(self, f, raw):
        sid, book = self._instrument(f, raw)
        self._oid += 1
        order = {'oid': str(76253459 + self._oid), 'report_id': f['11'], 'clordid': f['11'],
                 'account': party(raw, '24') or '', 'sid': sid,
                 'symbol': f.get('55', ''), 'exchange': f.get('207', ''),
                 'cfi': f.get('461', ''), 'maturity': f.get('200', ''),
                 'legs': legs_of(raw) if f['35'] == 'AB' else [],
                 'side': f['54'], 'qty': float(f['38']), 'cum': 0.0, 'type': f['40'],
                 'tif': f.get('59', '0'),
                 'price': float(f['44']) if f.get('44') else None}
        self.orders[order['oid']] = order
        if order['account'] not in self.accounts:
            self._order_report(order, '8', '8', t151=0, t103='99',
                               t58=f"Order entry transaction was rejected. Error: "
                                   f"account {order['account']} not found")
            return
        if self.reject_text:
            self._order_report(order, '8', '8', t151=0, t103='99', t58=self.reject_text)
            return
        if book is None:
            self._order_report(order, '8', '8', t151=0, t103='1',
                               t58='Order entry transaction was rejected. Error: unknown product')
            return
        tick = book['tick']
        if order['price'] is not None and abs(round(order['price'] / tick) * tick
                                              - order['price']) > 1e-9:
            self._order_report(order, '8', '8', t151=0, t103='99',
                               t58='Order entry transaction was rejected. Error: invalid price')
            return
        self._order_report(order, '0', '0', t151=order['qty'])
        buy = order['side'] == '1'
        touch = book['ask'] if buy else book['bid']
        if self.no_liquidity and order['tif'] == '3':
            self._order_report(order, '4', '4', t151=0)
        elif order['type'] == '1':                       # MARKET
            self._fill(order, touch)
        elif (buy and order['price'] >= touch) or (not buy and order['price'] <= touch):
            self._fill(order, touch)                     # marketable LIMIT
        elif order['tif'] == '3':                        # IOC: fill now or never
            self._order_report(order, '4', '4', t151=0)
        else:
            self.resting[order['oid']] = order           # rests in the book

    def _cancel(self, f):
        order = self.resting.get(f.get('37', ''))
        if order is None:
            self.deliver([('35', '9'), ('11', f['11']), ('37', f.get('37', '')),
                          ('39', '4' if f.get('37') in self.orders else '0'),
                          ('102', '99'), ('434', '1'),
                          ('58', 'Order cancel transaction was rejected. Error: order not found')])
            return
        self.resting.pop(order['oid'], None)
        order['report_id'] = f['11']                     # the cancel's ClOrdID; no 41
        self._order_report(order, '4', '4', t151=0)

    def _replace(self, f):
        order = self.resting.get(f.get('37', ''))
        if order is None:
            self.deliver([('35', '9'), ('11', f['11']), ('37', f.get('37', '')), ('39', '0'),
                          ('102', '99'), ('434', '2'),
                          ('58', 'Order cancel/replace transaction was rejected. '
                                 'Error: order not found')])
            return
        order['report_id'] = f['11']
        if f.get('44'):
            order['price'] = float(f['44'])
        if f.get('38'):
            order['qty'] = float(f['38'])
        self._order_report(order, '5', '0', t151=order['qty'] - order['cum'])
        book = self.books[order['sid']]
        buy = order['side'] == '1'
        touch = book['ask'] if buy else book['bid']
        if (buy and order['price'] >= touch) or (not buy and order['price'] <= touch):
            self._fill(order, touch)

    def _status(self, f):
        self.status_requests.append(dict(f))
        if self.status_silent:
            return
        order = self.orders.get(f.get('37', ''))
        if order is None:
            self.deliver([('35', '8'), ('11', f.get('11', '')), ('37', f.get('37', '')),
                          ('17', '0'), ('150', 'I'), ('39', '8'), ('151', '0'),
                          ('58', 'order not found'), ('790', f.get('790', ''))])
            return
        status = ('2' if order['cum'] >= order['qty'] else
                  '0' if order['oid'] in self.resting else '4')
        fields = self._base(order) + [('11', f.get('11', '')), ('17', '0'), ('150', 'I'),
                                      ('39', status), ('14', str(order['cum'])),
                                      ('151', str(order['qty'] - order['cum'])
                                       if status == '0' else '0'),
                                      ('790', f.get('790', ''))]
        self.deliver(fields)


    def _mass_status(self, f):
        """A Mass Order Status Request (AF): one report per order (all, or
        one product's with 585=3 / 55), each carrying 584, 911 and 912 — and,
        as in Stellar's own example (8.20), ExecType 5."""
        self.mass_requests.append(dict(f))
        if self.status_silent:
            return
        orders = [o for o in self.orders.values() if o['oid'] in self.resting
                  and (f.get('585') != '3' or o['symbol'] == f.get('55'))]
        for n, order in enumerate(orders, 1):
            self.deliver(self._base(order) + [
                ('11', order['report_id']), ('17', '0'), ('150', '5'), ('39', '0'),
                ('14', str(order['cum'])), ('151', str(order['qty'] - order['cum'])),
                ('584', f.get('584', '')), ('911', str(len(orders))),
                ('912', 'Y' if n == len(orders) else 'N')])
        if not orders:
            # Not in Stellar's spec: what it answers when nothing is working
            # is an open question (docs/FIX_NOTES.md). This fake says so in one
            # report with 911=0, so the sweep can finish.
            self.deliver([('35', '8'), ('17', '0'), ('150', 'I'), ('39', '0'),
                          ('584', f.get('584', '')), ('911', '0'), ('912', 'Y')])


class Peer:
    """One FIX connection to the exchange: the Order Router or Market Data."""

    def __init__(self, exchange, port=None):
        self.exchange = exchange
        self.port = port
        self.received = []
        self.logons = []
        self.seq = 0               # Market Data's own numbering
        self.comp = None
        self.kind = None
        self.subs = {}             # sid -> MD request id
        self.closed = False
        self.lock = threading.Lock()

    def settimeout(self, timeout):
        pass

    def _frame(self, seq, fields, possdup=False):
        header = [('35', fields[0][1]), ('34', str(seq)), ('49', 'STELLAR'), ('56', self.comp)]
        if possdup:
            header += [('43', 'Y'), ('122', time.strftime('%Y%m%d-%H:%M:%S.000', time.gmtime()))]
        return encode_fix_message(header + fields[1:])

    def reply(self, fields):
        with self.lock:
            if self.kind == 'OR':
                seq = self.exchange.or_record(self.comp, fields)
            else:
                self.seq += 1
                seq = self.seq
            self.received.append(self._frame(seq, fields))

    def resend(self, begin, end):
        session = self.exchange.or_sessions.get(self.comp) or {'seq': 0, 'sent': {}}
        last = end or session['seq']
        with self.lock:
            for seq in range(begin, last + 1):
                fields = session['sent'].get(seq)
                if fields is None or fields[0][1] in ('A', '0', '1', '2', '4', '5'):
                    self.received.append(self._frame(seq, [('35', '4'), ('123', 'Y'),
                                                           ('36', str(seq + 1))], possdup=True))
                else:
                    self.received.append(self._frame(seq, fields, possdup=True))

    def _levels(self, sid):
        book = self.exchange.books[sid]
        out = []
        if book['bid'] is not None:
            out += [('269', '0'), ('270', repr(book['bid'])), ('271', str(book['bid_size'])),
                    ('290', '1')]
        if book['ask'] is not None:
            out += [('269', '1'), ('270', repr(book['ask'])), ('271', str(book['ask_size'])),
                    ('290', '1')]
        return out

    def publish(self, sid):
        """A snapshot (W): 48 before the request id, as Stellar's example."""
        request = self.subs.get(sid)
        if request is None or sid not in self.exchange.books:
            return
        levels = self._levels(sid)
        self.reply([('35', 'W'), ('48', sid), ('262', request),
                    ('268', str(len(levels) // 4))] + levels)

    def publish_change(self, sid):
        """An Incremental Refresh (X): position 1 each side changed."""
        request = self.subs.get(sid)
        if request is None:
            return
        book = self.exchange.books[sid]
        groups = []
        for kind, side in (('0', 'bid'), ('1', 'ask')):
            if book[side] is not None:
                groups += [('279', '1'), ('48', sid), ('269', kind), ('270', repr(book[side])),
                           ('271', str(book[side + '_size'])), ('290', '1')]
        self.reply([('35', 'X'), ('262', request), ('268', str(len(groups) // 6))] + groups)

    def sendall(self, payload):
        raw = payload.decode('ascii')
        f = parse_fix_message(raw)
        self.comp = f['49']
        kind = f['35']
        if kind == 'A':
            self.kind = 'MD' if f['49'].endswith('_MD') or f['49'].startswith('MARKET') else 'OR'
            gap = None
            if self.kind == 'OR':
                Exchange._last_or_comp = f['49']
                session = self.exchange.or_sessions.setdefault(f['49'], {'seq': 0, 'sent': {}})
                if f.get('141') == 'Y':
                    session['seq'], session['sent'] = 0, {}
                    session['in_expected'] = 1
                expected = session.get('in_expected', 1)
                seq = int(f.get('34') or 0)
                if seq > expected:
                    gap = expected                 # numbers we never received
                session['in_expected'] = seq + 1
            self.logons.append(dict(f))
            self.reply([('35', 'A'), ('98', '0'), ('108', '30')]
                       + ([('141', 'Y')] if f.get('141') == 'Y' else []))
            if gap is not None:
                # As Stellar would: ask for what we skipped (answered by a GapFill).
                self.exchange.resend_requests_sent.append((f['49'], gap, seq - 1))
                self.reply([('35', '2'), ('7', str(gap)), ('16', '0')])
            return
        if self.kind == 'OR' and f.get('34', '').isdigit():
            session = self.exchange.or_sessions.get(f['49'])
            if session is not None:
                if kind == '4' and f.get('123') == 'Y':
                    self.exchange.gap_fills.append((f['49'], int(f['34']), int(f.get('36') or 0)))
                    session['in_expected'] = int(f.get('36') or 0)
                elif f.get('43') != 'Y':
                    session['in_expected'] = int(f['34']) + 1
        if kind == '5':
            self.exchange.logouts.append((self.kind, f.get('58', '')))
            if not self.exchange.logout_silent:
                self.reply([('35', '5'), ('58', 'Logout confirmed')])
            return
        if kind == '2' and self.kind == 'OR':
            self.resend(int(f.get('7', '1')), int(f.get('16', '0')))
            return
        if kind in ('0', '1', '4', '5'):
            return
        if self.kind == 'MD':
            if kind == 'V' and f.get('263') == '1':
                self.subs[f['48']] = f['262']
                self.publish(f['48'])
            elif kind == 'V' and f.get('263') == '2':
                self.subs.pop(f['48'], None)
            elif kind == 'x':
                self.exchange.security_list(self, f)
            return
        self.exchange.on_order(f, raw)

    def recv(self, size):
        with self.lock:
            if self.received:
                return self.received.pop(0)
        time.sleep(0.002)
        raise socket.timeout()

    def close(self):
        self.closed = True


def install(monkeypatch, exchange):
    """Every FIX connection the program opens goes to `exchange`."""
    def create(address=None, *a, **k):
        peer = Peer(exchange, port=int(address[1]) if address else None)
        exchange.peers.append(peer)
        return peer
    monkeypatch.setattr(socket, 'create_connection', create)
