"""Configuration for the 45-60 DTE S&P 500 credit-spread bot.

Secrets come from this folder's `.env` (never literal keys here):
    ALPACA_API_KEY=...
    ALPACA_SECRET_KEY=...
    ALPACA_PAPER=true            # false -> live trading URL
    ALPACA_BASE_URL=...          # optional override
    ALPACA_DATA_URL=...          # optional override
"""

import os
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")


def _env_flag(name: str, default: bool = True) -> bool:
    """Strict boolean from the environment. Unset or blank gives `default` (paper); anything that is not clearly true or
    false raises, so a typo can never switch a bot to live trading (it used to: any value but 1/true/yes meant LIVE)."""
    raw = os.getenv(name)
    value = (str(default) if raw is None or not raw.strip() else raw).strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{name}={raw!r} is not a recognised true/false value (use true or false)")

PAPER_TRADING = _env_flag("ALPACA_PAPER", True)
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL = os.getenv(
    "ALPACA_BASE_URL",
    "https://paper-api.alpaca.markets" if PAPER_TRADING else "https://api.alpaca.markets",
)
ALPACA_DATA_URL = os.getenv("ALPACA_DATA_URL", "https://data.alpaca.markets")

DRY_RUN = False
RISK_FREE_RATE = 0.04
CLOSE_SLIPPAGE = 0.05
CLOSE_RETRY_SEC = 15
CLOSE_MAX_RETRIES = 6
LOG_DIR = "logs"

UNIVERSE_FILE = "data/sp500_tickers.txt"
HISTORY_PERIOD = "1y"
DOWNLOAD_WORKERS = 8
RSI_FAST = 14
RSI_SLOW = 28
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
RSI_STRONG_OVERSOLD = 25
RSI_STRONG_OVERBOUGHT = 75

DTE_MIN = 45
DTE_MAX = 60
DTE_TARGET = 52
DTE_TOLERANCE_DAYS = 10
DTE_SEARCH_MIN = 30
DTE_SEARCH_MAX = 80
TARGET_DELTA = 0.30
DELTA_MIN = 0.20
DELTA_MAX = 0.40
SPREAD_WIDTHS = (5.0, 2.5, 1.0)   # tried in this order until a strike pair with live quotes exists
SPREAD_WIDTH = 5.0                # reference width: MIN_CREDIT* apply per $5 of width and scale down
MIN_CREDIT = 1.50
MIN_CREDIT_STRONG = 1.40

POSITION_SIZE_TIERS = {10_000: 3, 30_000: 5, 50_000: 10, float("inf"): 15}
MAX_RISK_PER_TRADE_PCT = 0.04
MAX_PORTFOLIO_RISK_PCT = 0.52
ALLOW_MIN_CONTRACT_OVERRIDE = True
MAX_NEW_POSITIONS_PER_DAY = 10
MAX_LOSS_HITS_CIRCUIT_BREAKER = 3
MAX_LOSS_HIT_PCT = 0.90
DAILY_LOSS_ALERT_PCT = 0.03

PROFIT_TARGET_PCT = 0.50
EXIT_DTE = 7
# 2026-10-08: adopt() takes over an untracked option spread only when it is on a universe name AND expires more than this
# many days out. The account may be shared with the 0DTE bot (SPXW, and its stock-ORB "F" spreads on 0-7 DTE expirations
# of the same big names): adopting those would put them under this bot's DTE<=EXIT_DTE exit and close them. Positions
# already in state.json are always kept, whatever their DTE.
ADOPT_MIN_DTE = 7
MAX_LOSS_EXIT = True
# 2026-09-25: motivated by a real trade that reached 0.48 (40% of max profit, entry 0.80, target
# 0.40 = 50% of max) then reversed straight through entry to 0.95 -- a near-winner that round-
# tripped to a loss with no giveback protection at all. Once a position reaches
# PROFIT_FLOOR_ARM_PCT_OF_TARGET of the way to the profit target (0.85 * 50% = 42.5% of max profit),
# it arms a HARD FLOOR at PROFIT_FLOOR_PCT_OF_MAX of max profit (30%) -- a fixed level, not a trail
# off the best price seen, so an armed position that gives back everything still exits with at
# least 30% rather than riding all the way back through entry.
PROFIT_FLOOR_ENABLED = True
PROFIT_FLOOR_ARM_PCT_OF_TARGET = 0.85
PROFIT_FLOOR_PCT_OF_MAX = 0.30
# 2026-10-02 exit-rule selectors (the Schwab folder overrides them; see its config_45dte.py). The
# defaults below reproduce the original behavior exactly.
#   EXIT_DTE_ENABLED False  -> never force-close on DTE; positions ride to expiration.
#   PROFIT_FLOOR_MODE "fixed" -> the hard floor at PROFIT_FLOOR_PCT_OF_MAX described above.
#   PROFIT_FLOOR_MODE "trail" -> once armed, exit at market on the first PROFIT_FLOOR_TRAIL_TICK
#     give-back from the best price seen, if that still books a profit (else disarm and ride).
EXIT_DTE_ENABLED = True
PROFIT_FLOOR_MODE = "fixed"
PROFIT_FLOOR_TRAIL_TICK = 0.05
# "trail" mode only sells when the mid price still shows at least this much profit (a market close
# fills at the natural price, up to a half-spread worse than the mid); otherwise it disarms and rides.
PROFIT_FLOOR_MIN_PROFIT = 0.05
# Manual overrides (thinkorswim). The owner may close or cancel positions by hand at any time, so every
# maintenance cycle the bot compares its tracked spreads with broker.get_positions() (sync_external) and
# follows what the broker really holds instead of fighting it. An inconsistent picture must repeat
# identically for EXTERNAL_SYNC_CONFIRM_CYCLES consecutive cycles before the bot acts on it (a positions API
# that lags a fill must never make it drop a real position); spreads younger than EXTERNAL_SYNC_MIN_AGE_SEC
# (since opened / last filled) are not checked. After a spread is found fully closed by hand, no NEW entry is
# submitted for MANUAL_CLOSE_COOLDOWN_SEC (the owner is mid-action), then trading resumes normally.
EXTERNAL_SYNC_ENABLED = True
EXTERNAL_SYNC_CONFIRM_CYCLES = 2
EXTERNAL_SYNC_MIN_AGE_SEC = 120
# Before an exit books a position as already closed by hand (or flags a naked leg), the bot reads the
# broker a second time this many seconds later and acts only if both reads agree -- one read can be
# empty or stale, and wrongly dropping a live position would drop its exit management.
EXTERNAL_EXIT_RECHECK_SEC = 2
# Official exit price (what realized P/L, the circuit breaker and the daily report run on):
#   "prev_tick" -- the mark one maintenance tick BEFORE the tick that triggered the exit, the same rule the 0DTE
#                  bots use. A paper fill (natural-price escalation, last-trade fills) can land far from what
#                  the strategy saw; the tick before the trigger is the stand-in for "the print before the
#                  close". With no usable prior tick the trigger mark itself is used. The broker's REAL fill is
#                  still recorded (fill_debit / realized_pl_actual_fill) next to it.
#   "fill"      -- the broker's actual fill (switch to this when the bot trades real money).
# A resting GTC profit-target close has no trigger tick: it books at its fill, never worse than its limit.
EXIT_PNL_BASIS = "prev_tick"
# A prior tick (or trigger mark) older than this many seconds is not "the tick before the close" -- e.g.
# yesterday's last mark on the first cycle of the day -- and is not used.
EXIT_PREV_TICK_MAX_AGE_SEC = 300
MANUAL_CLOSE_COOLDOWN_SEC = 120
ENTRY_TIME_IN_FORCE = "day"
PRICE_REDUCTION_INTERVAL_MIN = 60
PRICE_REDUCTION_AMOUNT = 0.02
CANCEL_UNFILLED_AT = "15:55"

SCAN_SCHEDULE = [("09:30", "11:30", 5), ("11:30", "15:00", 30), ("15:00", "16:00", 5)]
MAINTENANCE_INTERVAL_SEC = 60
REPORT_INTERVAL_MIN = 60
MARKET_OPEN = "09:30"
MARKET_CLOSE = "16:00"
EOD_TIME = "16:05"
LOOP_SLEEP_SEC = 15
# 2026-10-08: data-failure safety (the 0DTE bots have the same settings). A position not priced for MARK_STALE_ALERT_SEC raises
# a CRITICAL alert and halts new entries (no automatic close: the positions are long-dated and their exits rest at the
# broker). A tick that raises anything but BrokerError no longer ends the process. Schwab sim: the 7-day refresh token's age
# is checked every TOKEN_CHECK_SEC; entries stop when it would expire before the session ends (+ margin).
MARK_STALE_ALERT_SEC = 600
TICK_EXCEPTION_LOG_SEC = 300
TOKEN_CHECK_SEC = 60
TOKEN_WARN_DAYS = 5.0
TOKEN_ENTRY_MARGIN_MIN = 30
IDLE_LOG_INTERVAL_SEC = 300

MARKET_HOLIDAYS = [
    date(2026, 1, 1),
    date(2026, 1, 19),
    date(2026, 2, 16),
    date(2026, 4, 3),
    date(2026, 5, 25),
    date(2026, 6, 19),
    date(2026, 7, 3),
    date(2026, 9, 7),
    date(2026, 11, 26),
    date(2026, 12, 25),
]
