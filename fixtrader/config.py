"""Venues, contracts and settings in one JSON file.

Credentials never live in that file. Each venue names an environment variable
(`password_env`) holding its password, and the value lives in `.env`,
gitignored, written by the UI. Never in code, never in config, never in an API
response, never in a log line.

Three things in here carry weight beyond "read some settings":

- **Saves go through `atomicfile`.** See that module for the failure it exists
  to prevent.
- **A per-contract field left BLANK means "use the desk default", and 0 is a
  real number.** A loop that skips falsy values can only ever set an override,
  never clear one — and it silently ignores a commission of 0, a budget of 0
  and a limit of 0, all of which are genuine statements.
- **UAT and PROD are separate venues.** Nothing here merges them, and the
  environment is a required field with no default, so a venue saved without
  one is refused rather than assumed to be the safe kind.
"""

import os
import re
from typing import Any, Dict, List, Optional

from . import atomicfile
from .models import OrderType, TargetBasis, TimeInForce

try:
    from dotenv import load_dotenv
except ImportError:                      # optional; the shell can set them
    load_dotenv = None


ENVIRONMENTS = ("UAT", "PROD")
FIX_VERSIONS = ("FIX.4.2", "FIX.4.4", "FIXT.1.1")


# ---------------------------------------------------------------------------
# desk-wide defaults
#
# Every tunable the engine reads is here and is a visible field in the UI:
# a guessed number gets corrected from measurement, which needs it on screen
# first.
# ---------------------------------------------------------------------------

DEFAULT_SETTINGS: Dict[str, Any] = {
    # -- the loop ---------------------------------------------------------
    #: How often the SCREEN refreshes. The headline requirement, and a
    #: setting rather than a constant so it can be measured against.
    'PRICE_REFRESH_SEC': 0.5,
    #: How often the engine takes a pass. Faster than the screen: the
    #: statistics should not be sampled at the rate a human reads them.
    'ENGINE_POLL_SEC': 0.1,
    #: How often commands are drained, on their own thread. A switch flipped
    #: in the UI must not wait for a price poll.
    'COMMAND_POLL_SEC': 0.02,

    # -- master switches --------------------------------------------------
    #: OFF stands every contract's algo down at once. Deliberately not per
    #: contract: a switch you have to find eight times is one that gets
    #: missed once.
    'ALGO_MASTER_ENABLED': True,
    'CONFIRM_CLOSE': True,
    'SOUND_ENABLED': True,
    #: 'ask' / 'always' / 'never'. An unanswered prompt means NO.
    'SHUTDOWN_CLOSE_POSITIONS': 'ask',

    # -- guards -----------------------------------------------------------
    #: How long a quote may go unchanged before entries are withheld. 0 = off,
    #: which also stops it catching a feed that has genuinely STOPPED.
    'MAX_QUOTE_AGE_SEC': 15.0,
    'MAX_PRICE_JUMP_SIGMA': 5.0,
    'JUMP_SETTLE_SEC': 2.0,
    'REFUSE_OUTSIDE_SESSION': True,

    # -- desk limits ------------------------------------------------------
    'DAILY_MAX_LOSS_TOTAL': 0.0,
    'MAX_OPEN_CONTRACTS': 0.0,

    # -- defaults a blank contract field falls back to --------------------
    # -- the Algo: Bollinger bands on the contract's own candles ------------
    #: Candles of this many minutes, each closing on the last MID seen in
    #: it, the one forming now included. Built from the recorded mids — a
    #: FIX market-data session has no history to backfill from.
    'DEFAULT_TIMEFRAME_MIN': 15,
    #: N: the band's middle is EMA(N) of the closes, sigma the POPULATION
    #: standard deviation of the last N — TradingView's Bollinger, EMA basis.
    'DEFAULT_LENGTH': 20,
    #: ARMED when the BID's z reaches +this (H to L) or the OFFER's z
    #: reaches -this (L to H) — each side on the price it could trade.
    'DEFAULT_ENTRY_THRESHOLD': 2.5,
    #: No entry beyond this |z|: a blow-out, not a stretch. 0 = no cap.
    'DEFAULT_MAX_ENTRY_Z': 3.5,
    #: Fresh quotes in a row the entry condition must hold for.
    'DEFAULT_CONFIRM_SAMPLES': 3,
    #: BOTH, SELL_ONLY (H to L) or BUY_ONLY (L to H). Entries only.
    'DEFAULT_TRADE_DIRECTION': 'BOTH',
    #: RE-ENTRY: armed at the band, entered on the way back IN by
    #: `reentry_back` z, inside a window `reentry_window_pct` of the way
    #: back to the mean. A trend rides the band and never comes back.
    'DEFAULT_REENTRY_ON': True,
    'DEFAULT_REENTRY_BACK': 0.5,
    'DEFAULT_REENTRY_WINDOW_PCT': 50.0,
    #: TREND: no entry against a middle (EMA) that moved more than this many
    #: sigma over the lookback.
    'DEFAULT_TREND_ON': True,
    'DEFAULT_TREND_SIGMA': 1.0,
    'DEFAULT_TREND_LOOKBACK_MIN': 120.0,
    #: No entry this close to the session close (or after it). 0 = off.
    'DEFAULT_CUTOFF_BUFFER_MIN': 20.0,
    #: Minutes of LIVE prices the Algo must have watched since it was
    #: switched on before its first entry. 0 = off.
    'DEFAULT_WARMUP_MIN': 90.0,
    #: The filters. Edge: expected capture (capture x |z| x sigma, in money)
    #: at least `edge_multiple` x the round trip. Regime: no entry while the
    #: contract is TRENDING. Half-life band in minutes, 0 = that end off.
    'DEFAULT_EDGE_ON': True,
    'DEFAULT_EDGE_MULTIPLE': 1.5,
    'DEFAULT_EDGE_CAPTURE_FRAC': 0.5,
    'DEFAULT_REGIME_ON': True,
    'DEFAULT_REGIME_ER_MAX': 0.6,
    'DEFAULT_REGIME_MIN_CROSSINGS': 4,
    'DEFAULT_HALF_LIFE_MIN_MIN': 0.0,
    'DEFAULT_HALF_LIFE_MAX_MIN': 0.0,
    #: The STOP LOSS, from break-even: % of the margin (MARGIN) or a
    #: multiple of the ATR (ATR). On by default — a signal that says where
    #: to get out in profit and never in a loss is half an exit.
    'DEFAULT_STOP_LOSS_ON': True,
    'DEFAULT_STOP_LOSS_PCT': 2.0,
    'DEFAULT_STOP_MODE': 'MARGIN',
    'DEFAULT_TARGET_MODE': 'MARGIN',
    'DEFAULT_ATR_PERIOD': 14,
    'DEFAULT_ATR_STOP_MULT': 2.0,
    'DEFAULT_ATR_TARGET_MULT': 1.5,
    #: Per-direction levels (H to L = _HL, L to H = _LH). None is "same as
    #: both": the shared target / stop above stand until a direction asks.
    'DEFAULT_TARGET_MODE_HL': None,
    'DEFAULT_PROFIT_TARGET_PCT_HL': None,
    'DEFAULT_ATR_TARGET_MULT_HL': None,
    'DEFAULT_STOP_MODE_HL': None,
    'DEFAULT_STOP_LOSS_PCT_HL': None,
    'DEFAULT_ATR_STOP_MULT_HL': None,
    'DEFAULT_TARGET_MODE_LH': None,
    'DEFAULT_PROFIT_TARGET_PCT_LH': None,
    'DEFAULT_ATR_TARGET_MULT_LH': None,
    'DEFAULT_STOP_MODE_LH': None,
    'DEFAULT_STOP_LOSS_PCT_LH': None,
    'DEFAULT_ATR_STOP_MULT_LH': None,
    #: The optional exits, each OFF until a contract asks: a z stop on the
    #: closing side, back at the mean only in profit, and a time stop.
    'DEFAULT_STOP_Z_ON': False,
    'DEFAULT_STOP_LOSS_Z': 4.0,
    'DEFAULT_EXIT_AT_MEAN': False,
    'DEFAULT_MAX_HOLD_MINUTES': 0.0,
    'DEFAULT_MAX_LOSSES_ROW': 3,
    'DEFAULT_PROGRESS_BAR': True,
    # -- the touch study (Analysis): a rolling time window of the mids ------
    'DEFAULT_WINDOW_MINUTES': 150.0,
    'DEFAULT_MIN_HISTORY_MINUTES': 120.0,
    'DEFAULT_SAMPLE_INTERVAL_SEC': 1.0,
    'DEFAULT_STATS_UPDATE_INTERVAL_SEC': 300.0,
    #: The margin one contract ties up, as the operator enters it: TT does
    #: not report it. The target and the stop are percentages OF this, so a
    #: contract without one in MARGIN mode has no levels — and does not enter.
    'DEFAULT_MARGIN_PER_CONTRACT': 0.0,
    'DEFAULT_MIN_BOOK_SIZE': 0.0,
    'DEFAULT_MAX_BOOK_SPREAD_TICKS': 0.0,
    'DEFAULT_QUANTITY': 1.0,
    'DEFAULT_MAX_POSITION': 0.0,
    'DEFAULT_MAX_TRADES_PER_DAY': 10.0,
    'DEFAULT_DAILY_MAX_LOSS': 0.0,
    'DEFAULT_ENTRY_COOLDOWN_SECONDS': 300.0,
    'DEFAULT_ENTRY_ORDER_TYPE': OrderType.LIMIT.value,
    'DEFAULT_EXIT_ORDER_TYPE': OrderType.MARKET.value,
    'DEFAULT_ENTRY_LIMIT_OFFSET_TICKS': 1.0,
    'DEFAULT_EXIT_LIMIT_OFFSET_TICKS': 1.0,
    #: A MARKET order goes to the exchange as a LIMIT this many ticks
    #: THROUGH the touch (a buy at the offer + N, a sell at the bid - N),
    #: immediate-or-cancel: it fills now or not at all, like a market order,
    #: but with a price an exchange's price band accepts. CME (via TT) gives
    #: a bare market order its own protection price and REJECTS it when that
    #: is outside the band — a close that never happens. 0 sends a true
    #: market order.
    'DEFAULT_MARKET_LIMIT_TICKS': 2.0,
    'DEFAULT_ENTRY_LIMIT_TIMEOUT_SEC': 30.0,
    'DEFAULT_EXIT_LIMIT_TIMEOUT_SEC': 30.0,
    #: A missed ENTRY is a trade not taken; a missed EXIT is a position you
    #: still hold. That asymmetry is why these two defaults differ.
    'DEFAULT_ENTRY_ON_TIMEOUT': 'CANCEL',
    'DEFAULT_EXIT_ON_TIMEOUT': 'CROSS_AT_MARKET',
    #: Every amend loses queue position, so re-pricing on every pass
    #: guarantees you are never at the front of a queue.
    'DEFAULT_REPEG_DEAD_BAND_TICKS': 1.0,
    'DEFAULT_TIME_IN_FORCE': TimeInForce.DAY.value,
    #: Which close flag a closing order carries. 'CLOSE' is the plain offset
    #: flag and suits most venues. SHFE and INE price a close-today
    #: differently from a close-yesterday, so those contracts want
    #: 'CLOSE_TODAY', 'CLOSE_YESTERDAY', or 'AUTO' to pick from the trading
    #: day the position was opened on. It is never 'OPEN': an opposite order
    #: that does not say it is closing opens the other side instead.
    'DEFAULT_CLOSE_OFFSET_MODE': 'CLOSE',
    'DEFAULT_COMMISSION_PER_CONTRACT': 0.0,
    'DEFAULT_EXCHANGE_FEE_PER_CONTRACT': 0.0,
    'DEFAULT_CLEARING_FEE_PER_CONTRACT': 0.0,
    #: A BUDGET, not a measurement. Default 0: a fabricated cost is charged
    #: against every trade and the operator cannot tell it was never theirs.
    'DEFAULT_SLIPPAGE_BUDGET_TICKS': 0.0,
    #: Exit once NET P&L — after the whole round trip — reaches this % of
    #: the margin entered for the contract.
    'DEFAULT_PROFIT_TARGET_PCT': 2.0,

    # -- notifications ----------------------------------------------------
    'NOTIFY_ORDERS': False,
    'NOTIFY_FILLS': True,
    'NOTIFY_POSITIONS': True,
    'NOTIFY_REJECTS': True,
    'NOTIFY_WITHHELD': True,
    'TELEGRAM_ENABLED': False,
    'TELEGRAM_CHAT_ID': '',

    # -- storage ----------------------------------------------------------
    'DATABASE_PATH': 'fixtrader.db',
    'PERSIST_STATS_SAMPLES': True,
    #: A restart reloads the recorded window when the engine was off for
    #: less than this; a longer gap starts the window afresh.
    'RESUME_MAX_GAP_MINUTES': 120.0,
    'EVENT_RETENTION_DAYS': 90,
}

#: Read once at startup. Changing one needs a restart and must SAY so;
#: warning "restart" on every save teaches the operator to ignore the line
#: that matters.
STRUCTURAL_SETTINGS = ('PRICE_REFRESH_SEC', 'ENGINE_POLL_SEC',
                       'DATABASE_PATH')

#: Per-contract field -> the desk-wide default it falls back to when blank.
CONTRACT_DEFAULTS: Dict[str, str] = {
    'window_minutes': 'DEFAULT_WINDOW_MINUTES',
    'min_history_minutes': 'DEFAULT_MIN_HISTORY_MINUTES',
    'sample_interval_sec': 'DEFAULT_SAMPLE_INTERVAL_SEC',
    'stats_update_interval_sec': 'DEFAULT_STATS_UPDATE_INTERVAL_SEC',
    'entry_threshold': 'DEFAULT_ENTRY_THRESHOLD',
    'max_entry_z': 'DEFAULT_MAX_ENTRY_Z',
    'confirm_samples': 'DEFAULT_CONFIRM_SAMPLES',
    'trade_direction': 'DEFAULT_TRADE_DIRECTION',
    'stop_loss_z': 'DEFAULT_STOP_LOSS_Z',
    'max_hold_minutes': 'DEFAULT_MAX_HOLD_MINUTES',
    'exit_at_mean': 'DEFAULT_EXIT_AT_MEAN',
    'margin_per_contract': 'DEFAULT_MARGIN_PER_CONTRACT',
    'min_book_size': 'DEFAULT_MIN_BOOK_SIZE',
    'max_book_spread_ticks': 'DEFAULT_MAX_BOOK_SPREAD_TICKS',
    'quantity': 'DEFAULT_QUANTITY',
    'max_position': 'DEFAULT_MAX_POSITION',
    'max_trades_per_day': 'DEFAULT_MAX_TRADES_PER_DAY',
    'daily_max_loss': 'DEFAULT_DAILY_MAX_LOSS',
    'entry_cooldown_seconds': 'DEFAULT_ENTRY_COOLDOWN_SECONDS',
    'entry_order_type': 'DEFAULT_ENTRY_ORDER_TYPE',
    'exit_order_type': 'DEFAULT_EXIT_ORDER_TYPE',
    'entry_limit_offset_ticks': 'DEFAULT_ENTRY_LIMIT_OFFSET_TICKS',
    'exit_limit_offset_ticks': 'DEFAULT_EXIT_LIMIT_OFFSET_TICKS',
    'market_limit_ticks': 'DEFAULT_MARKET_LIMIT_TICKS',
    'entry_limit_timeout_sec': 'DEFAULT_ENTRY_LIMIT_TIMEOUT_SEC',
    'exit_limit_timeout_sec': 'DEFAULT_EXIT_LIMIT_TIMEOUT_SEC',
    'entry_on_timeout': 'DEFAULT_ENTRY_ON_TIMEOUT',
    'exit_on_timeout': 'DEFAULT_EXIT_ON_TIMEOUT',
    'repeg_dead_band_ticks': 'DEFAULT_REPEG_DEAD_BAND_TICKS',
    'time_in_force': 'DEFAULT_TIME_IN_FORCE',
    'close_offset_mode': 'DEFAULT_CLOSE_OFFSET_MODE',
    'commission_per_contract': 'DEFAULT_COMMISSION_PER_CONTRACT',
    'exchange_fee_per_contract': 'DEFAULT_EXCHANGE_FEE_PER_CONTRACT',
    'clearing_fee_per_contract': 'DEFAULT_CLEARING_FEE_PER_CONTRACT',
    'slippage_budget_ticks': 'DEFAULT_SLIPPAGE_BUDGET_TICKS',
    'profit_target_pct': 'DEFAULT_PROFIT_TARGET_PCT',
    'timeframe_min': 'DEFAULT_TIMEFRAME_MIN',
    'length': 'DEFAULT_LENGTH',
    'reentry_on': 'DEFAULT_REENTRY_ON',
    'reentry_back': 'DEFAULT_REENTRY_BACK',
    'reentry_window_pct': 'DEFAULT_REENTRY_WINDOW_PCT',
    'trend_on': 'DEFAULT_TREND_ON',
    'trend_sigma': 'DEFAULT_TREND_SIGMA',
    'trend_lookback_min': 'DEFAULT_TREND_LOOKBACK_MIN',
    'cutoff_buffer_min': 'DEFAULT_CUTOFF_BUFFER_MIN',
    'warmup_min': 'DEFAULT_WARMUP_MIN',
    'edge_on': 'DEFAULT_EDGE_ON',
    'edge_multiple': 'DEFAULT_EDGE_MULTIPLE',
    'edge_capture_frac': 'DEFAULT_EDGE_CAPTURE_FRAC',
    'regime_on': 'DEFAULT_REGIME_ON',
    'regime_er_max': 'DEFAULT_REGIME_ER_MAX',
    'regime_min_crossings': 'DEFAULT_REGIME_MIN_CROSSINGS',
    'half_life_min_min': 'DEFAULT_HALF_LIFE_MIN_MIN',
    'half_life_max_min': 'DEFAULT_HALF_LIFE_MAX_MIN',
    'stop_loss_on': 'DEFAULT_STOP_LOSS_ON',
    'stop_loss_pct': 'DEFAULT_STOP_LOSS_PCT',
    'stop_mode': 'DEFAULT_STOP_MODE',
    'target_mode': 'DEFAULT_TARGET_MODE',
    'atr_period': 'DEFAULT_ATR_PERIOD',
    'atr_stop_mult': 'DEFAULT_ATR_STOP_MULT',
    'atr_target_mult': 'DEFAULT_ATR_TARGET_MULT',
    'target_mode_hl': 'DEFAULT_TARGET_MODE_HL',
    'profit_target_pct_hl': 'DEFAULT_PROFIT_TARGET_PCT_HL',
    'atr_target_mult_hl': 'DEFAULT_ATR_TARGET_MULT_HL',
    'stop_mode_hl': 'DEFAULT_STOP_MODE_HL',
    'stop_loss_pct_hl': 'DEFAULT_STOP_LOSS_PCT_HL',
    'atr_stop_mult_hl': 'DEFAULT_ATR_STOP_MULT_HL',
    'target_mode_lh': 'DEFAULT_TARGET_MODE_LH',
    'profit_target_pct_lh': 'DEFAULT_PROFIT_TARGET_PCT_LH',
    'atr_target_mult_lh': 'DEFAULT_ATR_TARGET_MULT_LH',
    'stop_mode_lh': 'DEFAULT_STOP_MODE_LH',
    'stop_loss_pct_lh': 'DEFAULT_STOP_LOSS_PCT_LH',
    'atr_stop_mult_lh': 'DEFAULT_ATR_STOP_MULT_LH',
    'stop_z_on': 'DEFAULT_STOP_Z_ON',
    'max_losses_row': 'DEFAULT_MAX_LOSSES_ROW',
    'progress_bar': 'DEFAULT_PROGRESS_BAR',
}


def env_key_for(name: str) -> str:
    """The .env variable name for a venue. Sanitised, upper case, prefixed."""
    slug = re.sub(r'[^A-Za-z0-9]+', '_', str(name or '')).strip('_').upper()
    return f"FIX_{slug}" if slug else "FIX_UNNAMED"


def _blank_to_none(value: Any) -> Optional[float]:
    """A number from the UI, where blank means "unset" and 0 does not."""
    if value is None or value == '':
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _blank_to_none_any(value: Any) -> Any:
    """As above, for a field that is not a number (a mode, a type)."""
    if value is None or value == '':
        return None
    return value


def database_path(config, status_path: str) -> str:
    """The ONE database file of a desk. A relative DATABASE_PATH lives beside
    the status file (the runtime folder), for the engine that writes it and
    the screen that reads it alike — resolved apart, the screen read an empty
    file in the program folder while the engine wrote runtime/fixtrader.db."""
    path = config.settings.get('DATABASE_PATH', 'fixtrader.db')
    if os.path.isabs(path):
        return path
    return os.path.join(os.path.dirname(os.path.abspath(status_path)),
                        os.path.basename(path))


class VenueConfig:
    """One FIX session: one endpoint, one set of comp ids, one environment.

    The password is NOT here. `password_env` names the variable; the value
    lives in `.env` and is read through the `password` property, which is the
    only place in the system that can see it.
    """

    def __init__(self, name: str, environment: str = "UAT",
                 broker: str = "", host: str = "", port: Optional[int] = None,
                 md_host: str = "", md_port: Optional[int] = None,
                 sender_comp_id: str = "", target_comp_id: str = "",
                 sender_sub_id: str = "", target_sub_id: str = "",
                 on_behalf_of_comp_id: str = "",
                 fix_version: str = "FIX.4.4", username: str = "",
                 password_env: str = "", account: str = "",
                 heartbeat_sec: int = 30, reset_seq_on_logon: bool = True,
                 use_tls: bool = True, data_dictionary: str = "",
                 store_path: str = "", log_path: str = "",
                 enabled: bool = True, on_behalf_of_sub_id: str = "",
                 md_sender_comp_id: str = "", md_target_comp_id: str = "",
                 md_password_env: str = "",
                 recovery_host: str = "", recovery_port: Optional[int] = None):
        self.name = name
        self.environment = self._environment(environment, name)
        self.broker = broker
        self.host = host
        self.port = int(port) if port else None
        self.on_behalf_of_sub_id = on_behalf_of_sub_id
        self.md_sender_comp_id = md_sender_comp_id
        self.md_target_comp_id = md_target_comp_id
        self.md_password_env = md_password_env
        self.md_host = md_host
        self.md_port = int(md_port) if md_port else None
        #: TT FIX Recovery for the Order Routing session: the SAME login on
        #: its own address. Blank = derived from the Order Routing endpoint.
        self.recovery_host = recovery_host
        self.recovery_port = int(recovery_port) if recovery_port else None
        self.sender_comp_id = sender_comp_id
        self.target_comp_id = target_comp_id
        self.sender_sub_id = sender_sub_id
        self.target_sub_id = target_sub_id
        self.on_behalf_of_comp_id = on_behalf_of_comp_id
        self.fix_version = fix_version
        self.username = username
        self.password_env = password_env or env_key_for(name)
        self.account = account
        self.heartbeat_sec = int(heartbeat_sec or 30)
        self.reset_seq_on_logon = bool(reset_seq_on_logon)
        self.use_tls = bool(use_tls)
        self.data_dictionary = data_dictionary
        self.store_path = store_path
        self.log_path = log_path
        self.enabled = bool(enabled)

    @staticmethod
    def _environment(value: Any, name: str) -> str:
        """UAT or PROD, and nothing else.

        There is no default. A venue whose environment is missing or
        unrecognised is REFUSED rather than assumed to be the safe kind —
        assuming UAT would eventually assume it about a live one.
        """
        env = str(value or '').strip().upper()
        if env not in ENVIRONMENTS:
            raise ValueError(
                f"venue '{name}': environment is {value!r}; it must be "
                f"one of {', '.join(ENVIRONMENTS)}. UAT and PROD are separate "
                f"venues with separate credentials and neither is a default.")
        return env

    #: TT's FIX Recovery (Order Routing) beside each Order Routing endpoint:
    #: direct TLS on 11502 -> 11508, through stunnel 11702 -> 11708.
    RECOVERY_PORTS = {11502: 11508, 11702: 11708}

    def recovery_endpoint(self):
        """(host, port) of FIX Recovery for the Order Routing session, or
        (None, None) when it cannot be told — said, never guessed."""
        if self.recovery_host and self.recovery_port:
            return self.recovery_host, self.recovery_port
        host, port = self.host or '', self.port
        if port not in self.RECOVERY_PORTS:
            return None, None
        if 'fixorderrouting-' in host:
            host = host.replace('fixorderrouting-', 'fixrecovery-')
        elif host not in ('127.0.0.1', 'localhost'):
            return None, None
        return self.recovery_host or host, self.recovery_port or self.RECOVERY_PORTS[port]

    @property
    def is_production(self) -> bool:
        return self.environment == "PROD"

    @property
    def password(self) -> Optional[str]:
        """The one place the secret is readable. Never returned by an API."""
        return os.environ.get(self.password_env) if self.password_env else None

    @property
    def has_password(self) -> bool:
        return bool(self.password)

    def to_dict(self) -> Dict[str, Any]:
        """For config.json. Carries the .env KEY, never the value."""
        return {
            'environment': self.environment, 'broker': self.broker,
            'host': self.host, 'port': self.port,
            'md_host': self.md_host, 'md_port': self.md_port,
            'recovery_host': self.recovery_host, 'recovery_port': self.recovery_port,
            'md_sender_comp_id': self.md_sender_comp_id,
            'md_target_comp_id': self.md_target_comp_id,
            'md_password_env': self.md_password_env,
            'on_behalf_of_sub_id': self.on_behalf_of_sub_id,
            'sender_comp_id': self.sender_comp_id,
            'target_comp_id': self.target_comp_id,
            'sender_sub_id': self.sender_sub_id,
            'target_sub_id': self.target_sub_id,
            'on_behalf_of_comp_id': self.on_behalf_of_comp_id,
            'fix_version': self.fix_version, 'username': self.username,
            'password_env': self.password_env, 'account': self.account,
            'heartbeat_sec': self.heartbeat_sec,
            'reset_seq_on_logon': self.reset_seq_on_logon,
            'use_tls': self.use_tls, 'data_dictionary': self.data_dictionary,
            'store_path': self.store_path, 'log_path': self.log_path,
            'enabled': self.enabled,
        }

    def to_public_dict(self) -> Dict[str, Any]:
        """For the UI. The password is reported as SET or NOT SET and never
        as a value — not even a masked one that could be echoed back."""
        d = self.to_dict()
        d['name'] = self.name
        d['password_set'] = self.has_password
        return d

    @classmethod
    def from_dict(cls, name: str, raw: Dict[str, Any]) -> "VenueConfig":
        raw = dict(raw or {})
        raw.pop('name', None)
        raw.pop('password_set', None)
        raw.pop('password', None)        # never accepted from a file
        return cls(name=name, **raw)


class ContractConfig:
    """One window on the screen: one spread contract at one venue.

    Specifications (tick size, tick value, multiplier, size bounds) are READ
    FROM THE VENUE and cached here so the screen can render before the session
    is up. `spec_source` records where each came from, because a number the
    operator typed and a number the venue reported must be distinguishable —
    every money figure on the window runs through them.
    """

    def __init__(self, key: str, name: str = "", symbol: str = "",
                 venue: str = "", security_id: str = "",
                 security_exchange: str = "",
                 tick_size: Any = None, tick_value: Any = None,
                 contract_multiplier: Any = None, currency: str = "USD",
                 min_qty: Any = None, qty_step: Any = None, max_qty: Any = None,
                 session_open: str = "", session_close: str = "",
                 decimals: int = 4, enabled: bool = True,
                 algo_on: bool = False,
                 spec_source: Optional[Dict[str, str]] = None,
                 **overrides: Any):
        self.key = key
        self.name = name or key
        self.symbol = symbol
        self.venue = venue
        self.security_id = security_id
        self.security_exchange = security_exchange

        self.tick_size = _blank_to_none(tick_size)
        self.tick_value = _blank_to_none(tick_value)
        self.contract_multiplier = _blank_to_none(contract_multiplier)
        self.currency = currency or "USD"
        self.min_qty = _blank_to_none(min_qty)
        self.qty_step = _blank_to_none(qty_step)
        self.max_qty = _blank_to_none(max_qty)

        self.session_open = session_open
        self.session_close = session_close
        self.decimals = int(decimals or 4)
        self.enabled = bool(enabled)
        #: Whether the algo is armed. Persisted so a restart does not silently
        #: re-arm a contract the trader stood down — or stand down one they
        #: left running.
        self.algo_on = bool(algo_on)
        self.spec_source = dict(spec_source or {})

        #: Per-contract overrides. None means "use the desk default", and 0 is
        #: a real number — see `settings_with_defaults`.
        self.overrides: Dict[str, Any] = {}
        for field in CONTRACT_DEFAULTS:
            if field in overrides:
                self.overrides[field] = _blank_to_none_any(overrides[field])

    def settings_with_defaults(self, desk: Dict[str, Any]) -> Dict[str, Any]:
        """This contract's effective settings: its own where set, the desk's
        where blank. `0` is set; `None` and `''` are blank."""
        out: Dict[str, Any] = {}
        for field, default_key in CONTRACT_DEFAULTS.items():
            value = self.overrides.get(field)
            if value is None:
                value = desk.get(default_key, DEFAULT_SETTINGS.get(default_key))
            out[field] = value
        # Booleans arrive from JSON as bools already; numbers may be strings
        # from a form post, so coerce the ones that are always numeric.
        for numeric in ('window_minutes', 'min_history_minutes',
                        'sample_interval_sec', 'stats_update_interval_sec',
                        'entry_threshold', 'max_entry_z', 'confirm_samples',
                        'stop_loss_z', 'max_hold_minutes',
                        'timeframe_min', 'length', 'reentry_back',
                        'reentry_window_pct', 'trend_sigma',
                        'trend_lookback_min', 'cutoff_buffer_min',
                        'warmup_min', 'edge_multiple', 'edge_capture_frac',
                        'regime_er_max', 'regime_min_crossings',
                        'half_life_min_min', 'half_life_max_min',
                        'stop_loss_pct', 'atr_period', 'atr_stop_mult',
                        'atr_target_mult', 'max_losses_row',
                        'margin_per_contract', 'min_book_size',
                        'max_book_spread_ticks', 'quantity', 'max_position',
                        'max_trades_per_day', 'daily_max_loss',
                        'entry_cooldown_seconds', 'entry_limit_offset_ticks',
                        'exit_limit_offset_ticks', 'entry_limit_timeout_sec',
                        'exit_limit_timeout_sec', 'repeg_dead_band_ticks',
                        'market_limit_ticks',
                        'commission_per_contract', 'exchange_fee_per_contract',
                        'clearing_fee_per_contract', 'slippage_budget_ticks',
                        'profit_target_pct',
                        'profit_target_pct_hl', 'atr_target_mult_hl',
                        'stop_loss_pct_hl', 'atr_stop_mult_hl',
                        'profit_target_pct_lh', 'atr_target_mult_lh',
                        'stop_loss_pct_lh', 'atr_stop_mult_lh'):
            try:
                out[numeric] = float(out[numeric]) if out[numeric] is not None else None
            except (TypeError, ValueError):
                out[numeric] = DEFAULT_SETTINGS.get(CONTRACT_DEFAULTS[numeric])
        out['confirm_samples'] = max(1, int(out['confirm_samples'] or 1))
        # An unrecognised direction means BOTH — never a silent refusal to
        # trade.
        direction = str(out.get('trade_direction') or 'BOTH').upper()
        out['trade_direction'] = (direction if direction in
                                  ('BOTH', 'SELL_ONLY', 'BUY_ONLY') else 'BOTH')
        for flag in ('exit_at_mean', 'reentry_on', 'trend_on', 'edge_on',
                     'regime_on', 'stop_loss_on', 'stop_z_on',
                     'progress_bar'):
            value = out.get(flag)
            out[flag] = (value if isinstance(value, bool) else
                         str(value).strip().lower() in ('1', 'true', 'yes', 'on'))
        for mode in ('stop_mode', 'target_mode'):
            chosen = str(out.get(mode) or 'MARGIN').upper()
            out[mode] = chosen if chosen in ('MARGIN', 'ATR') else 'MARGIN'
        # A direction's own mode: blank (or anything unknown) is "same as both".
        for mode in ('stop_mode_hl', 'target_mode_hl', 'stop_mode_lh', 'target_mode_lh'):
            chosen = str(out.get(mode) or '').upper()
            out[mode] = chosen if chosen in ('MARGIN', 'ATR') else None
        return out

    def to_dict(self) -> Dict[str, Any]:
        d = {
            'name': self.name, 'symbol': self.symbol, 'venue': self.venue,
            'security_id': self.security_id,
            'security_exchange': self.security_exchange,
            'tick_size': self.tick_size, 'tick_value': self.tick_value,
            'contract_multiplier': self.contract_multiplier,
            'currency': self.currency, 'min_qty': self.min_qty,
            'qty_step': self.qty_step, 'max_qty': self.max_qty,
            'session_open': self.session_open,
            'session_close': self.session_close,
            'decimals': self.decimals, 'enabled': self.enabled,
            'algo_on': self.algo_on, 'spec_source': self.spec_source,
        }
        d.update(self.overrides)
        return d

    @classmethod
    def from_dict(cls, key: str, raw: Dict[str, Any]) -> "ContractConfig":
        return cls(key=key, **dict(raw or {}))


class TraderConfig:
    """The whole configuration: settings, venues and contracts."""

    def __init__(self, path: str = "config.json",
                 settings: Optional[Dict[str, Any]] = None,
                 venues: Optional[Dict[str, VenueConfig]] = None,
                 contracts: Optional[Dict[str, ContractConfig]] = None):
        self.path = path
        self.settings = dict(DEFAULT_SETTINGS)
        self.settings.update(settings or {})
        self.venues: Dict[str, VenueConfig] = dict(venues or {})
        self.contracts: Dict[str, ContractConfig] = dict(contracts or {})

    # -- loading and saving ----------------------------------------------

    @classmethod
    def from_file(cls, path: str = "config.json",
                  env_path: str = ".env") -> "TraderConfig":
        if load_dotenv is not None and os.path.exists(env_path):
            load_dotenv(env_path, override=False)
        raw = atomicfile.read_json(path, default={}) or {}
        cfg = cls(path=path, settings=raw.get('settings') or {})
        for name, vraw in (raw.get('venues') or {}).items():
            cfg.venues[name] = VenueConfig.from_dict(name, vraw)
        for key, craw in (raw.get('contracts') or {}).items():
            cfg.contracts[key] = ContractConfig.from_dict(key, craw)
        return cfg

    def to_dict(self) -> Dict[str, Any]:
        return {
            'settings': self.settings,
            'venues': {n: v.to_dict() for n, v in self.venues.items()},
            'contracts': {k: c.to_dict() for k, c in self.contracts.items()},
        }

    def save(self, path: Optional[str] = None) -> None:
        atomicfile.write_json(path or self.path, self.to_dict())

    # -- accessors -------------------------------------------------------

    def contract(self, key: str) -> Optional[ContractConfig]:
        return self.contracts.get(key)

    def venue(self, name: str) -> Optional[VenueConfig]:
        return self.venues.get(name)

    def enabled_contracts(self) -> List[ContractConfig]:
        return [c for c in self.contracts.values() if c.enabled]

    def effective(self, key: str) -> Dict[str, Any]:
        c = self.contracts.get(key)
        return c.settings_with_defaults(self.settings) if c else {}

    @property
    def has_production_venue(self) -> bool:
        """Whether ANY enabled venue is live. The taskbar badge reads this,
        and it is deliberately true if even one is."""
        return any(v.is_production and v.enabled for v in self.venues.values())

    @property
    def environment_label(self) -> str:
        if self.has_production_venue:
            return "PROD"
        return "UAT" if self.venues else "SIMULATED"

    def structural_changes(self, new_settings: Dict[str, Any]) -> List[str]:
        """Which of the incoming settings need a restart to take effect."""
        return [k for k in STRUCTURAL_SETTINGS
                if k in new_settings and new_settings[k] != self.settings.get(k)]


def write_env_value(key: str, value: str, env_path: str = ".env") -> None:
    """Set one variable in `.env`, leaving the rest of the file alone.

    The only writer of secrets in the system. It rewrites the whole file
    atomically because a partial `.env` is a venue that cannot log on.
    """
    lines: List[str] = []
    found = False
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")
    atomicfile.write_text(env_path, "\n".join(lines) + "\n")
    os.environ[key] = value
