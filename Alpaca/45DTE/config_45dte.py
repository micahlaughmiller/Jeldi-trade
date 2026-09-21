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

PAPER_TRADING = os.getenv("ALPACA_PAPER", "true").strip().lower() in ("1", "true", "yes")
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
MAX_LOSS_EXIT = True
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
