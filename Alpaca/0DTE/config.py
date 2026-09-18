"""0DTE SPX credit-spread bot configuration.

Secrets come from the folder's .env (python-dotenv); this file holds no keys.
All times are US/Eastern wall-clock.
"""

import os
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

ET = ZoneInfo("US/Eastern")

# The selected persona determines both the Alpaca credentials and the names of
# all local state/log files. Run one process per persona/account.
TRADER_NAMES = (
    "ASTRA", "CLAUDE", "JAMES", "SARAH", "MARCUS", "ELENA", "DAVID", "ARIA", "JORDAN",
)
ACTIVE_TRADER = os.getenv("ACTIVE_TRADER", "ASTRA").strip().upper()
if ACTIVE_TRADER not in TRADER_NAMES:
    raise ValueError(
        f"Unknown ACTIVE_TRADER: {ACTIVE_TRADER}. "
        f"Choose one of {', '.join(TRADER_NAMES)}"
    )
ALPACA_API_KEY = os.getenv(f"{ACTIVE_TRADER}_API_KEY", "").strip()
ALPACA_SECRET_KEY = os.getenv(f"{ACTIVE_TRADER}_API_SECRET", "").strip()
TRADER_NAME = ACTIVE_TRADER

PAPER_TRADING = os.getenv("PAPER_TRADING", "true").lower() in ("1", "true", "yes")
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

UNDERLYING = "SPX"
OPTION_ROOT = "SPXW"
SPX_SYMBOL = "^GSPC"
ES_SYMBOL = "ES=F"

MARKET_OPEN = time(9, 30)
OVERNIGHT_SESSION_START = time(18, 0)
OPENING_RANGE_MINUTES = 15
OVERNIGHT_ENTRY_END_MIN = 30
LAST_ENTRY_TIME = time(12, 0)
FORCE_CLOSE_TIME = time(12, 30)

TICK_SECONDS = 15
CANDLE_INTERVAL = "2m"
CANDLE_LOOKBACK_MIN = 180
DATA_CACHE_SEC = 20

BREAKOUT_LEVEL_SOURCE = "ES_TO_SPX"

WIDTH_BY_TIER = {1: 5, 2: 5, 3: 10, 4: 10}
CREDIT_RANGE_BY_WIDTH = {5: (2.75, 3.50), 10: (5.50, 7.00)}
TIER_BANDS = [(10_000, 1), (30_000, 2), (50_000, 3), (float("inf"), 4)]
RISK_PER_TRADE_PCT = 0.05
ALLOW_MIN_CONTRACT_OVERRIDE = True
MAX_PORTFOLIO_RISK_PCT = 0.52
MAX_CONTRACTS_PER_TRADE = 10
MAX_TRADES_PER_DAY = 5
MAX_CONSECUTIVE_LOSSES = 2
DAILY_LOSS_LIMIT_PCT = 0.10
MAX_CONCURRENT_POSITIONS = 1

PROFIT_TARGET = 0.30
STOP_LOSS = 0.30
RUNNER_ENABLED = True
RUNNER_MIN_CONTRACTS = 2
TRAIL_AMOUNT = 0.50
MOMENTUM_CANDLES = 3
MOMENTUM_CONTINUE_RATIO = 0.80
MOMENTUM_SLOWDOWN_PCT = 0.20

ENTRY_STEP_SEC = 20
ENTRY_PRICE_STEP = 0.05
ENTRY_TIMEOUT_SEC = 120

NEWS_DAYS = [
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
]
NEWS_DAY_MODE = "half_size"
