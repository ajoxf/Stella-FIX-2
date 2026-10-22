"""A small fake TT exchange, spoken to over the REAL FIX sessions.

Both sessions — Order Routing and Market Data — connect to it through a
monkeypatched `socket.create_connection`, so everything between the screen
and the wire is the production code. It keeps a book per Security ID and
behaves as an exchange does for the cases a desk relies on:

- Market Data: a Market Data Request (V) is answered with a snapshot (W),
  and every `move()` of the market publishes a fresh one;
- New Order Single (D): acknowledged (150=0); a MARKET fills at the touch;
  a LIMIT that crosses fills at the touch, otherwise it RESTS and fills at
  its own price when the market reaches it (`move`);
- Cancel (F) and Cancel/Replace (G) of a resting order; a cancel for an
  order it does not know is a Cancel Reject (9) in its own words;
- an unknown account is rejected (150=8) with tag 58, as TT does;
- Request For Positions (AN): AO + one AP per instrument held, from the
  fills it made;
- Security Definition Request (c): a definition (d) with TT's tick (969),
  the exchange's tick (16552) and the DisplayFactor (9787);
- FIX Recovery (a connection to a port in `recovery_ports`): the same
  login, News "Recovery is complete", then ONE Recovery Request (U2) —
  18002=Y replays the reports the program never received (`drop_reports`),
  916/917 every report in the window; both together is a Business Reject
  (j) in TT's words; then Logout "Recovery completed ...".

Prices here are TT's FIX prices — CL as 9057 for 90.57 — as TT sends them.

It is a stand-in for UAT in the cloud, not a model of TT's matching.
"""
import calendar
import socket
import threading
import time

from fixtrader.gateway import encode_fix_message, parse_fix_message

SOH = '\x01'


class Exchange:
    def __init__(self, accounts=('ACC1',)):
        self.accounts = set(accounts)
        self.books = {}            # sid -> {'bid','ask','bid_size','ask_size','tick'}
        self.resting = {}          # clordid -> order dict
        self.positions = {}        # (account, sid) -> net qty
        self.peers = []
        self.lock = threading.RLock()
        self._exec = 0
        self._oid = 0
        #: Every message the program SENT, by ClOrdID — the tags it put on
        #: the wire, for a test to read back.
        self.orders_in = []
        #: When set, every new order is REJECTED with these words — as CME
        #: via TT rejects an order priced outside its band.
        self.reject_text = None
        #: The book is SHOWN but nothing at it trades — a thin UAT market:
        #: an immediate-or-cancel order is cancelled unfilled.
        self.no_liquidity = False
        #: Every Execution Report sent on Order Routing, in order:
        #: (time, fields, delivered). `drop_reports` > 0 loses the next N on
        #: the way — the program never sees them — as a line that was down.
        self.history = []
        self.drop_reports = 0
        self.recovery_ports = {11508, 11708}
        #: FIX Recovery says nothing back (a service not switched on).
        self.recovery_silent = False
        #: FIX Recovery refuses with these words (a Business Reject, j).
        self.recovery_reject = None
        self.recovery_requests = []

    # -- the market ----------------------------------------------------------

    def list(self, sid, bid, ask, tick=0.01, size=50, factor=None,
             symbol='CL', exchange='CME', point_value=1000):
        """An instrument, its book in FIX units, its tick (969) in FIX units
        and its DisplayFactor (9787): the exchange's tick is tick x factor."""
        self.books[sid] = {'bid': bid, 'ask': ask, 'bid_size': size,
                           'ask_size': size, 'tick': tick, 'factor': factor,
                           'symbol': symbol, 'exchange': exchange,
                           'point_value': point_value}

    def define(self, peer, f):
        sid = f.get('48', '')
        book = self.books.get(sid)
        if book is None:
            return
        fields = [('35', 'd'), ('320', f.get('320', '')), ('322', 'D1'),
                  ('323', '4'), ('48', sid), ('22', '96'),
                  ('55', book['symbol']), ('207', book['exchange']),
                  ('167', 'MLEG'), ('200', '202612'), ('107', book['symbol'] + ' spread'),
                  ('15', 'USD'), ('969', repr(book['tick'])),
                  ('16554', str(book['point_value']))]
        if book['factor'] is not None:
            fields += [('9787', repr(book['factor'])),
                       ('16552', format(book['tick'] * book['factor'], 'g'))]
        peer.reply(fields)

    def move(self, sid, bid, ask):
        """The market moves: publish it, and fill what it reached."""
        with self.lock:
            book = self.books[sid]
            book['bid'], book['ask'] = bid, ask
            for peer in self.peers:
                peer.publish(sid)
            for cid, order in list(self.resting.items()):
                if order['sid'] != sid:
                    continue
                if order['side'] == '1' and order['price'] >= ask:
                    self._fill(order, order['price'])
                elif order['side'] == '2' and order['price'] <= bid:
                    self._fill(order, order['price'])

    # -- helpers ------------------------------------------------------------------

    def next_exec(self, prefix):
        self._exec += 1
        return f'{prefix}{self._exec:06d}'

    def or_peer(self):
        return [p for p in self.peers if p.kind == 'OR'][-1]

    def _report(self, order, **extra):
        fields = [('35', '8'), ('11', order['current']),
                  ('37', order['oid']), ('1', order['account']),
                  ('48', order['sid']), ('55', order['symbol']),
                  ('54', order['side']), ('38', str(order['qty'])),
                  ('40', order['type']), ('60', time.strftime('%Y%m%d-%H:%M:%S.000', time.gmtime()))]
        if order.get('77'):
            fields.append(('77', order['77']))
        if order.get('orig'):
            fields.append(('41', order['orig']))
        for tag, value in extra.items():
            fields.append((tag.lstrip('t'), str(value)))
        fields.append(('75', time.strftime('%Y%m%d', time.gmtime())))
        delivered = self.drop_reports <= 0
        self.history.append([time.time(), fields, delivered])
        if delivered:
            self.or_peer().reply(fields)
        else:
            self.drop_reports -= 1

    def recover(self, peer, f):
        """A Recovery Request (U2), answered as TT does."""
        self.recovery_requests.append(dict(f))
        if self.recovery_silent:
            return
        if self.recovery_reject:
            peer.reply([('35', 'j'), ('45', f.get('34', '')), ('372', 'U2'), ('380', '0'),
                        ('58', self.recovery_reject)])
            return
        if f.get('18002') and (f.get('916') or f.get('917')):
            peer.reply([('35', 'j'), ('45', f.get('34', '')), ('372', 'U2'), ('380', '0'),
                        ('58', 'Provide either StartDate(916) and EndDate(917) or '
                               'CustomMode(18002) in OutOfBandRecoveryRequest message')])
            return
        with self.lock:
            if f.get('18002'):
                replay = [h for h in self.history if not h[2]]
            else:
                def stamp(v):
                    return calendar.timegm(time.strptime(v, '%Y%m%d-%H:%M:%S'))
                start, end = stamp(f['916']), stamp(f['917']) + 1
                replay = [h for h in self.history if start <= h[0] <= end]
            for h in replay:
                h[2] = True
                peer.reply(h[1])
        peer.reply([('35', '5'), ('58', 'Recovery completed for fix-session='
                                       'client_comp_id=' + (peer.comp or '') + '/TT/1'),
                    ('18000', '1')])

    def _fill(self, order, price):
        qty = order['qty'] - order['cum']
        order['cum'] += qty
        key = (order['account'], order['sid'])
        sign = 1 if order['side'] == '1' else -1
        self.positions[key] = self.positions.get(key, 0) + sign * qty
        self.resting.pop(order['current'], None)
        self._report(order, t17=self.next_exec('X'), t150='2', t39='2',
                     t32=qty, t31=price, t14=order['cum'], t151=0, t6=price)

    # -- the order session ----------------------------------------------------------

    def on_order(self, f):
        with self.lock:
            kind = f['35']
            if kind in ('D', 'F', 'G'):
                self.orders_in.append(dict(f))
            if kind == 'D':
                self._new(f)
            elif kind == 'F':
                self._cancel(f)
            elif kind == 'G':
                self._replace(f)
            elif kind == 'AN':
                self._positions(f)

    def _new(self, f):
        sid = f.get('48', '')
        self._oid += 1
        order = {'current': f['11'], 'oid': f'TT{self._oid:05d}',
                 'account': f.get('1', ''), 'sid': sid,
                 'symbol': f.get('55', ''), 'side': f['54'],
                 'qty': float(f['38']), 'cum': 0.0, 'type': f['40'],
                 'price': float(f['44']) if f.get('44') else None,
                 '77': f.get('77')}
        if order['account'] not in self.accounts:
            self._report(order, t17=self.next_exec('J'), t150='8', t39='8',
                         t14=0, t151=0,
                         t58=f"Account {order['account']} is not found")
            return
        if self.reject_text:
            self._report(order, t17=self.next_exec('J'), t150='8', t39='8',
                         t14=0, t151=0, t58=self.reject_text)
            return
        book = self.books.get(sid)
        if book is None:
            self._report(order, t17=self.next_exec('J'), t150='8', t39='8',
                         t14=0, t151=0, t58='Instrument not open for trading')
            return
        tick = book['tick']
        if order['price'] is not None and abs(round(order['price'] / tick) * tick
                                              - order['price']) > 1e-9:
            self._report(order, t17=self.next_exec('J'), t150='8', t39='8',
                         t14=0, t151=0, t58='Price is not a multiple of the tick')
            return
        self._report(order, t17=self.next_exec('A'), t150='0', t39='0',
                     t14=0, t151=order['qty'])
        buy = order['side'] == '1'
        touch = book['ask'] if buy else book['bid']
        if self.no_liquidity and f.get('59') == '3':
            self._report(order, t17=self.next_exec('C'), t150='4', t39='4',
                         t14=0, t151=0)
        elif order['type'] == '1':                       # MARKET
            self._fill(order, touch)
        elif (buy and order['price'] >= touch) or (not buy and order['price'] <= touch):
            self._fill(order, touch)                     # marketable LIMIT
        elif f.get('59') == '3':                         # IOC: fill now or never
            self._report(order, t17=self.next_exec('C'), t150='4', t39='4',
                         t14=0, t151=0)
        else:
            self.resting[order['current']] = order       # rests in the book

    def _find(self, f):
        return self.resting.get(f.get('41', '')) or next(
            (o for o in self.resting.values() if o['oid'] == f.get('37')), None)

    def _cancel(self, f):
        order = self._find(f)
        if order is None:
            self.or_peer().reply([('35', '9'), ('11', f['11']),
                                  ('41', f.get('41', '')), ('37', f.get('37', 'NONE')),
                                  ('39', '8'), ('434', '1'), ('102', '1'),
                                  ('58', 'Unknown order')])
            return
        self.resting.pop(order['current'], None)
        order['orig'], order['current'] = order['current'], f['11']
        self._report(order, t17=self.next_exec('C'), t150='4', t39='4',
                     t14=order['cum'], t151=0)

    def _replace(self, f):
        order = self._find(f)
        if order is None:
            self.or_peer().reply([('35', '9'), ('11', f['11']),
                                  ('41', f.get('41', '')), ('39', '8'),
                                  ('434', '2'), ('58', 'Unknown order')])
            return
        self.resting.pop(order['current'], None)
        order['orig'], order['current'] = order['current'], f['11']
        if f.get('44'):
            order['price'] = float(f['44'])
        if f.get('38'):
            order['qty'] = float(f['38'])
        self.resting[order['current']] = order
        self._report(order, t17=self.next_exec('R'), t150='5', t39='0',
                     t44=order['price'], t14=order['cum'],
                     t151=order['qty'] - order['cum'])

    def _positions(self, f):
        held = [(sid, q) for (acct, sid), q in self.positions.items()
                if acct == f.get('1') and q]
        peer = self.or_peer()
        peer.reply([('35', 'AO'), ('710', f['710']), ('728', '0'),
                    ('727', str(len(held))), ('1', f.get('1', ''))])
        for sid, q in held:
            peer.reply([('35', 'AP'), ('710', f['710']), ('727', str(len(held))),
                        ('48', sid), ('702', '1'), ('703', 'TQ'),
                        ('704', str(max(q, 0))), ('705', str(max(-q, 0)))])


class Peer:
    """One FIX connection to the exchange: Order Routing or Market Data."""

    def __init__(self, exchange, port=None):
        self.exchange = exchange
        self.port = port
        self.received = []
        self.seq = 1
        self.comp = None
        self.kind = None
        self.subs = {}             # sid -> MD request id
        self.closed = False
        self.lock = threading.Lock()

    def settimeout(self, timeout):
        pass

    def reply(self, fields):
        with self.lock:
            self.seq += 1
            msg = encode_fix_message([('35', fields[0][1]), ('34', str(self.seq)),
                                      ('49', 'TT'), ('56', self.comp)] + fields[1:])
            self.received.append(msg)

    def publish(self, sid):
        request = self.subs.get(sid)
        book = self.exchange.books.get(sid)
        if request is None or book is None:
            return
        self.reply([('35', 'W'), ('262', request), ('48', sid), ('268', '2'),
                    ('269', '0'), ('270', repr(book['bid'])), ('271', str(book['bid_size'])),
                    ('269', '1'), ('270', repr(book['ask'])), ('271', str(book['ask_size']))])

    def sendall(self, payload):
        f = parse_fix_message(payload.decode('ascii'))
        self.comp = f['49']
        kind = f['35']
        if kind == 'A':
            self.kind = ('REC' if self.port in self.exchange.recovery_ports else
                         'MD' if f['49'].startswith('MARKET') else 'OR')
            with self.lock:
                self.received.append(encode_fix_message(
                    [('35', 'A'), ('34', '1'), ('49', 'TT'), ('56', f['49']),
                     ('98', '0'), ('108', '30')]))
            # TT: "Recovery is complete" once the logon is done.
            self.reply([('35', 'B'), ('148', 'Recovery Complete'), ('33', '1'),
                        ('58', 'Recovery is complete')])
            return
        if self.kind == 'REC':
            if kind == 'U2':
                self.exchange.recover(self, f)
            return
        if self.kind == 'MD':
            if kind == 'V' and f.get('263') == '1':
                self.subs[f['48']] = f['262']
                self.publish(f['48'])
            elif kind == 'c':
                self.exchange.define(self, f)
            return
        self.exchange.on_order(f)

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
