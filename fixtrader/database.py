"""SQLite: positions, orders, fills, touches, events — and the samples that
let a restart warm up without waiting for a fresh window.

WAL with a 30-second busy timeout, because the web process reads while the
engine writes and neither may block the other into a timeout that reads on
screen as a dead engine.

The one keying rule worth stating: **fills are keyed (venue, exec_id)**. Two
venues' identical execution ids must not overwrite each other, and a system
that keys on exec_id alone loses a fill the first time a second venue is
added — silently, and only for the overlapping ids.
"""

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .models import (ExitReason, Position, Side, TouchEvent, TouchState)

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_key TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    opened_qty REAL,
    avg_price REAL NOT NULL,
    opened_at TEXT,
    entry_z REAL, entry_mean REAL, entry_std REAL, entry_half_life REAL,
    margin_locked REAL,
    break_even REAL, target_price REAL, stop_price REAL,
    closed_at TEXT, exit_price REAL, exit_z REAL, exit_reason TEXT,
    gross_pnl REAL, fees_paid REAL, net_pnl REAL, pnl_pct_on_margin REAL,
    is_simulated INTEGER DEFAULT 0,
    tickets TEXT DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS ix_positions_key ON positions(contract_key);
CREATE INDEX IF NOT EXISTS ix_positions_open ON positions(closed_at);

CREATE TABLE IF NOT EXISTS orders (
    clordid TEXT PRIMARY KEY,
    venue_id TEXT, contract_key TEXT, side TEXT, qty REAL, filled_qty REAL,
    price REAL, order_type TEXT, intent TEXT, state TEXT, text TEXT,
    reason TEXT, position_id INTEGER, sent_at TEXT, updated_at TEXT,
    sent_at_touch REAL, is_simulated INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_orders_key ON orders(contract_key);

CREATE TABLE IF NOT EXISTS fills (
    venue TEXT NOT NULL,
    exec_id TEXT NOT NULL,
    clordid TEXT, contract_key TEXT, side TEXT, qty REAL, price REAL,
    fees REAL, slippage_ticks REAL, venue_ts TEXT, our_ts TEXT,
    PRIMARY KEY (venue, exec_id)
);
CREATE INDEX IF NOT EXISTS ix_fills_key ON fills(contract_key);

CREATE TABLE IF NOT EXISTS sd_touches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_key TEXT NOT NULL, ts TEXT NOT NULL,
    level REAL, direction TEXT, price REAL, z REAL, mean REAL, std REAL,
    half_life REAL, algo_armed INTEGER, became_trade INTEGER DEFAULT 0,
    state TEXT DEFAULT 'UNRESOLVED', resolved_at TEXT,
    seconds_to_revert REAL, adverse_sigma REAL
);
CREATE INDEX IF NOT EXISTS ix_touches_key ON sd_touches(contract_key, level);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, contract_key TEXT, kind TEXT, text TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(id);

CREATE TABLE IF NOT EXISTS samples (
    contract_key TEXT NOT NULL, ts TEXT NOT NULL, price REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_samples_key ON samples(contract_key, ts);

-- Every fill TT reported on the Order Routing session, ours or not, in
-- TT's own fields. Display only: the book is built from `fills`.
CREATE TABLE IF NOT EXISTS tt_fills (
    exec_id TEXT PRIMARY KEY, clordid TEXT, orig_clordid TEXT, order_id TEXT,
    tt_time TEXT, account TEXT, security_id TEXT, symbol TEXT,
    contract_key TEXT, side TEXT, open_close TEXT, qty REAL, price REAL,
    cum_qty REAL, leaves_qty REAL, exec_type TEXT, ord_status TEXT,
    text TEXT, ours TEXT, received TEXT
);
CREATE INDEX IF NOT EXISTS ix_tt_fills_received ON tt_fills(received);

CREATE TABLE IF NOT EXISTS algo_warmup (
    contract_key TEXT PRIMARY KEY, live_sec REAL NOT NULL, at REAL NOT NULL
);

-- The TT display factor (9787) the recorded prices of a contract are in.
-- No row: recorded before prices were converted, i.e. in TT's FIX units.
CREATE TABLE IF NOT EXISTS price_units (
    contract_key TEXT PRIMARY KEY, factor REAL NOT NULL, at TEXT
);
"""


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _dt(text: Optional[str]) -> Optional[datetime]:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


class Database:
    def __init__(self, path: str = "fixtrader.db"):
        self.path = path
        self._lock = threading.Lock()
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    #: Columns added after a book could already exist. Added, never dropped:
    #: a book written by an older build keeps every row it had.
    ADDED_COLUMNS = {'positions': [('entry_slippage', 'REAL'),
                                   ('exit_slippage', 'REAL'),
                                   ('entry_order_type', 'TEXT'),
                                   ('exit_order_type', 'TEXT')]}

    def _migrate(self, conn) -> None:
        for table, columns in self.ADDED_COLUMNS.items():
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            for name, kind in columns:
                if name not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
        conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30.0,
                               check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    # -- positions -------------------------------------------------------

    def save_position(self, pos: Position) -> int:
        """Written on EVERY change: the book has to survive a crash between
        the fill and the next poll."""
        with self._lock, self._connect() as conn:
            row = (pos.contract_key, pos.side.value, pos.qty,
                   pos.opened_qty or pos.qty, pos.avg_price,
                   _iso(pos.opened_at), pos.entry_z, pos.entry_mean,
                   pos.entry_std, pos.entry_half_life, pos.margin_locked,
                   pos.break_even, pos.target_price, pos.stop_price,
                   _iso(pos.closed_at), pos.exit_price, pos.exit_z,
                   pos.exit_reason.value if pos.exit_reason else None,
                   pos.gross_pnl, pos.fees_paid, pos.net_pnl,
                   pos.pnl_pct_on_margin, int(pos.is_simulated),
                   json.dumps(pos.tickets), pos.entry_slippage,
                   pos.exit_slippage, pos.entry_order_type,
                   pos.exit_order_type)
            if pos.id is None:
                cur = conn.execute(
                    "INSERT INTO positions (contract_key, side, qty,"
                    " opened_qty, avg_price,"
                    " opened_at, entry_z, entry_mean, entry_std,"
                    " entry_half_life, margin_locked, break_even, target_price,"
                    " stop_price, closed_at, exit_price, exit_z, exit_reason,"
                    " gross_pnl, fees_paid, net_pnl, pnl_pct_on_margin,"
                    " is_simulated, tickets, entry_slippage, exit_slippage,"
                    " entry_order_type, exit_order_type) VALUES ("
                    + ",".join("?" * 28) + ")",
                    row)
                pos.id = cur.lastrowid
            else:
                conn.execute(
                    "UPDATE positions SET contract_key=?, side=?, qty=?,"
                    " opened_qty=?, avg_price=?, opened_at=?, entry_z=?,"
                    " entry_mean=?,"
                    " entry_std=?, entry_half_life=?, margin_locked=?,"
                    " break_even=?, target_price=?, stop_price=?, closed_at=?,"
                    " exit_price=?, exit_z=?, exit_reason=?, gross_pnl=?,"
                    " fees_paid=?, net_pnl=?, pnl_pct_on_margin=?,"
                    " is_simulated=?, tickets=?, entry_slippage=?,"
                    " exit_slippage=?, entry_order_type=?, exit_order_type=?"
                    " WHERE id=?",
                    row + (pos.id,))
            conn.commit()
        return pos.id

    @staticmethod
    def _position_from_row(r: sqlite3.Row) -> Position:
        return Position(
            id=r['id'], contract_key=r['contract_key'], side=Side(r['side']),
            qty=r['qty'], opened_qty=(r['opened_qty'] or r['qty']),
            avg_price=r['avg_price'],
            opened_at=_dt(r['opened_at']), entry_z=r['entry_z'],
            entry_mean=r['entry_mean'], entry_std=r['entry_std'],
            entry_half_life=r['entry_half_life'],
            margin_locked=r['margin_locked'], break_even=r['break_even'],
            target_price=r['target_price'], stop_price=r['stop_price'],
            closed_at=_dt(r['closed_at']), exit_price=r['exit_price'],
            exit_z=r['exit_z'],
            exit_reason=ExitReason(r['exit_reason']) if r['exit_reason'] else None,
            gross_pnl=r['gross_pnl'], fees_paid=r['fees_paid'],
            net_pnl=r['net_pnl'], pnl_pct_on_margin=r['pnl_pct_on_margin'],
            is_simulated=bool(r['is_simulated']),
            tickets=json.loads(r['tickets'] or '[]'),
            entry_slippage=r['entry_slippage'],
            exit_slippage=r['exit_slippage'],
            entry_order_type=r['entry_order_type'],
            exit_order_type=r['exit_order_type'])

    def open_positions(self) -> List[Position]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM positions WHERE closed_at IS NULL").fetchall()
        return [self._position_from_row(r) for r in rows]

    def positions_opened_since(self, since=None) -> List[Position]:
        """Every position opened at or after `since` (all when None), open
        or closed — the slippage report's window."""
        sql = "SELECT * FROM positions"
        args: List[Any] = []
        if since is not None:
            sql += " WHERE opened_at >= ?"
            args.append(since.isoformat() if hasattr(since, 'isoformat')
                        else str(since))
        sql += " ORDER BY opened_at DESC"
        with self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [self._position_from_row(r) for r in rows]

    def closed_positions(self, contract_key: Optional[str] = None,
                         limit: int = 500,
                         include_simulated: bool = True,
                         only_simulated: bool = False) -> List[Position]:
        sql = "SELECT * FROM positions WHERE closed_at IS NOT NULL"
        args: List[Any] = []
        if contract_key:
            sql += " AND contract_key = ?"
            args.append(contract_key)
        if only_simulated:
            sql += " AND is_simulated = 1"
        elif not include_simulated:
            sql += " AND is_simulated = 0"
        sql += " ORDER BY closed_at DESC LIMIT ?"
        args.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [self._position_from_row(r) for r in rows]

    def margin_per_contract(self, contract_key: str) -> Optional[float]:
        """What the venue charged per contract on this contract's most
        recent position that recorded a margin, or None.

        The replay prices a MARGIN target off this. It is read from what was
        actually locked up, never from a setting, and None when nothing was
        recorded — a guess would price every replayed target at a size
        nobody was charged.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT margin_locked, COALESCE(opened_qty, qty) AS size"
                " FROM positions WHERE contract_key = ?"
                " AND margin_locked > 0 AND COALESCE(opened_qty, qty) > 0"
                " ORDER BY id DESC LIMIT 1", (contract_key,)).fetchone()
        if row is None:
            return None
        return float(row['margin_locked']) / float(row['size'])

    # -- orders and fills -------------------------------------------------

    def save_order(self, order: Dict[str, Any]) -> None:
        cols = ('clordid', 'venue_id', 'contract_key', 'side', 'qty',
                'filled_qty', 'price', 'order_type', 'intent', 'state', 'text',
                'reason', 'position_id', 'sent_at', 'updated_at',
                'sent_at_touch', 'is_simulated')
        values = [order.get(c) for c in cols]
        with self._lock, self._connect() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO orders ({','.join(cols)}) "
                f"VALUES ({','.join('?' * len(cols))})", values)
            conn.commit()

    def order_intent(self, clordid: str) -> Optional[str]:
        """What an order of ours was FOR, as recorded when it was sent —
        'OPEN' or 'CLOSE' — or None if this book never sent it."""
        with self._connect() as conn:
            row = conn.execute("SELECT intent FROM orders WHERE clordid = ?",
                               (clordid,)).fetchone()
        return row['intent'] if row is not None else None

    def save_fill(self, fill) -> None:
        """INSERT OR IGNORE: a venue that resends an execution report must not
        double-count the fill."""
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO fills (venue, exec_id, clordid,"
                " contract_key, side, qty, price, fees, slippage_ticks,"
                " venue_ts, our_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (fill.venue, fill.exec_id, fill.clordid, fill.contract_key,
                 fill.side.value, fill.qty, fill.price, fill.fees,
                 fill.slippage_ticks, _iso(fill.venue_ts), _iso(fill.our_ts)))
            conn.commit()

    def has_fill(self, venue: str, exec_id: str) -> bool:
        """Is this fill already in the book? A report TT replays (FIX
        Recovery, a resend) must never be applied to a position twice."""
        with self._connect() as conn:
            return conn.execute("SELECT 1 FROM fills WHERE venue = ? AND exec_id = ?",
                                (venue, exec_id)).fetchone() is not None

    def fills(self, contract_key: Optional[str] = None,
              limit: int = 500) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM fills"
        args: List[Any] = []
        if contract_key:
            sql += " WHERE contract_key = ?"
            args.append(contract_key)
        sql += " ORDER BY our_ts DESC LIMIT ?"
        args.append(limit)
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    TT_FILL_COLUMNS = ('exec_id', 'clordid', 'orig_clordid', 'order_id',
                       'tt_time', 'account', 'security_id', 'symbol',
                       'contract_key', 'side', 'open_close', 'qty', 'price',
                       'cum_qty', 'leaves_qty', 'exec_type', 'ord_status',
                       'text', 'ours', 'received')

    def save_tt_fills(self, rows: List[Dict[str, Any]]) -> None:
        """The TT fills tape. INSERT OR IGNORE: a resent report is one fill."""
        if not rows:
            return
        cols = self.TT_FILL_COLUMNS
        with self._lock, self._connect() as conn:
            conn.executemany(
                f"INSERT OR IGNORE INTO tt_fills ({','.join(cols)}) "
                f"VALUES ({','.join('?' * len(cols))})",
                [[r.get(c) for c in cols] for r in rows])
            conn.commit()

    def tt_fills(self, limit: int = 500) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM tt_fills ORDER BY received DESC, rowid DESC"
                " LIMIT ?", (limit,)).fetchall()]

    def orders(self, contract_key: Optional[str] = None,
               limit: int = 500) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM orders"
        args: List[Any] = []
        if contract_key:
            sql += " WHERE contract_key = ?"
            args.append(contract_key)
        sql += " ORDER BY sent_at DESC LIMIT ?"
        args.append(limit)
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    # -- touches ----------------------------------------------------------

    def save_touch(self, touch: TouchEvent) -> int:
        with self._lock, self._connect() as conn:
            if touch.id is None:
                cur = conn.execute(
                    "INSERT INTO sd_touches (contract_key, ts, level,"
                    " direction, price, z, mean, std, half_life, algo_armed,"
                    " became_trade, state, resolved_at, seconds_to_revert,"
                    " adverse_sigma) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (touch.contract_key, _iso(touch.ts), touch.level,
                     touch.direction, touch.price, touch.z, touch.mean,
                     touch.std, touch.half_life, int(touch.algo_armed),
                     int(touch.became_trade), touch.state.value,
                     _iso(touch.resolved_at), touch.seconds_to_revert,
                     touch.adverse_sigma))
                touch.id = cur.lastrowid
            else:
                conn.execute(
                    "UPDATE sd_touches SET state=?, resolved_at=?,"
                    " seconds_to_revert=?, adverse_sigma=?, became_trade=?"
                    " WHERE id=?",
                    (touch.state.value, _iso(touch.resolved_at),
                     touch.seconds_to_revert, touch.adverse_sigma,
                     int(touch.became_trade), touch.id))
            conn.commit()
        return touch.id

    def touches(self, contract_key: Optional[str] = None,
                limit: int = 5000) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM sd_touches"
        args: List[Any] = []
        if contract_key:
            sql += " WHERE contract_key = ?"
            args.append(contract_key)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    # -- events and samples ------------------------------------------------

    def log_event(self, kind: str, text: str, contract_key: str = "",
                  detail: Any = None, ts: Optional[datetime] = None) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO events (ts, contract_key, kind, text, detail)"
                " VALUES (?,?,?,?,?)",
                (_iso(ts or datetime.now(timezone.utc)), contract_key, kind,
                 text, json.dumps(detail) if detail is not None else None))
            conn.commit()

    def events(self, since_id: int = 0, limit: int = 200,
               contract_key: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM events WHERE id > ?"
        args: List[Any] = [since_id]
        if contract_key:
            sql += " AND contract_key = ?"
            args.append(contract_key)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    # -- the Algo's warm-up ------------------------------------------------

    def warmup(self, key: str):
        """(seconds of live prices watched, when the last one counted, as
        UTC seconds), or None — so a quick restart carries the warm-up."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT live_sec, at FROM algo_warmup WHERE contract_key = ?",
                (key,)).fetchone()
        return None if row is None else (row['live_sec'], row['at'])

    def save_warmup(self, key: str, live_sec: float, at: float) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO algo_warmup (contract_key, live_sec, at)"
                " VALUES (?,?,?) ON CONFLICT(contract_key) DO UPDATE SET"
                " live_sec = excluded.live_sec, at = excluded.at",
                (key, float(live_sec), float(at)))
            conn.commit()

    def clear_warmup(self, key: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM algo_warmup WHERE contract_key = ?",
                         (key,))
            conn.commit()

    def price_unit(self, key: str) -> Optional[float]:
        """The display factor this contract's recorded prices are in, or
        None: recorded in TT's FIX units, before prices were converted."""
        with self._connect() as conn:
            row = conn.execute("SELECT factor FROM price_units WHERE "
                               "contract_key = ?", (key,)).fetchone()
        return float(row['factor']) if row else None

    def rescale_prices(self, key: str, ratio: float, factor: float) -> int:
        """Bring one contract's recorded prices to a new unit — the samples
        the band is rebuilt from and the touch study's readings — and record
        the factor they are now in. One transaction: all or nothing."""
        with self._lock, self._connect() as conn:
            n = conn.execute("UPDATE samples SET price = price * ? WHERE "
                             "contract_key = ?", (ratio, key)).rowcount
            conn.execute("UPDATE sd_touches SET price = price * ?, mean = "
                         "mean * ?, std = std * ? WHERE contract_key = ?",
                         (ratio, ratio, ratio, key))
            conn.execute("INSERT OR REPLACE INTO price_units VALUES (?,?,?)",
                         (key, factor, datetime.now(timezone.utc).isoformat()))
            conn.commit()
        return n

    def save_samples(self, key: str, rows: List[tuple]) -> None:
        if not rows:
            return
        with self._lock, self._connect() as conn:
            conn.executemany(
                "INSERT INTO samples (contract_key, ts, price) VALUES (?,?,?)",
                [(key, _iso(ts), price) for ts, price in rows])
            conn.commit()

    def recent_samples(self, key: str, limit: int = 400) -> List[float]:
        """Oldest-first, so the window can be refilled in order."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT price FROM samples WHERE contract_key = ?"
                " ORDER BY ts DESC LIMIT ?", (key, limit)).fetchall()
        return [r['price'] for r in reversed(rows)]

    def samples_between(self, key: str, since=None, until=None,
                        limit: int = 200000):
        """Recorded mids for one contract, oldest first, as (ts, price).

        `recent_samples` returns prices alone for warming a window on
        restart; a replay needs the clock too, because a time stop and a
        cooldown are measured in seconds and not in rows.
        """
        from datetime import datetime as _dt
        sql = "SELECT ts, price FROM samples WHERE contract_key = ?"
        args: list = [key]
        if since is not None:
            sql += " AND ts >= ?"
            args.append(since.isoformat() if hasattr(since, 'isoformat')
                        else str(since))
        if until is not None:
            sql += " AND ts <= ?"
            args.append(until.isoformat() if hasattr(until, 'isoformat')
                        else str(until))
        sql += " ORDER BY ts ASC LIMIT ?"
        args.append(int(limit))
        rows = []
        with self._connect() as conn:
            for row in conn.execute(sql, args):
                ts, price = row['ts'], row['price']
                try:
                    rows.append((_dt.fromisoformat(ts), float(price)))
                except (TypeError, ValueError):
                    continue          # a row we cannot read is skipped, not zeroed
        return rows

    def trim_samples(self, key: str, keep: int = 2000) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM samples WHERE contract_key = ? AND rowid NOT IN "
                "(SELECT rowid FROM samples WHERE contract_key = ?"
                " ORDER BY ts DESC LIMIT ?)", (key, key, keep))
            conn.commit()
