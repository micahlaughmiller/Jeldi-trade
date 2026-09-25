"""Configuration for the 45-60 DTE S&P 500 credit-spread bot (Schwab).

Secrets come from this folder's `.env` (never literal keys here):
    SCHWAB_APP_KEY=...
    SCHWAB_APP_SECRET=...
    SCHWAB_CALLBACK_URL=https://127.0.0.1:8080
    SCHWAB_ACCOUNT_INDEX=0
    SCHWAB_MODE=sim              # sim | dry_run | live
    SIM_STARTING_EQUITY=30000    # simulated account size for sim mode
    SCHWAB_LIVE_ORDERS=false     # true -> real orders are submitted (live mode only)

Schwab has NO paper-trading API: every submitted order is real money. SCHWAB_MODE
picks how orders are handled: `sim` (default) trades a local simulated account
filled against live Schwab quotes (paper_sim.py), `dry_run` only logs order
payloads, and `live` sends real orders -- but only when SCHWAB_LIVE_ORDERS=true is
also set; otherwise live is forced back to dry-run.
Strategy parameters below are identical to Alpaca/45DTE/config_45dte.py; only
the broker section differs. Run tools/check_sync.py to confirm.
"""

import os
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

SCHWAB_APP_KEY = os.getenv("SCHWAB_APP_KEY", "")
SCHWAB_APP_SECRET = os.getenv("SCHWAB_APP_SECRET", "")
SCHWAB_CALLBACK_URL = os.getenv("SCHWAB_CALLBACK_URL", "https://127.0.0.1:8080")
SCHWAB_TOKEN_PATH = os.getenv("SCHWAB_TOKEN_PATH", str(Path(__file__).resolve().parent / "token.json"))
SCHWAB_ACCOUNT_INDEX = int(os.getenv("SCHWAB_ACCOUNT_INDEX", "0"))
PAPER_TRADING = False

# Optional. Schwab serves no option chain for some indexes; with Alpaca keys present,
# index option quotes and expirations are read from Alpaca's indicative feed instead.
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
ALPACA_DATA_URL = os.getenv("ALPACA_DATA_URL", "https://data.alpaca.markets")

SCHWAB_MODE = os.getenv("SCHWAB_MODE", "sim").strip().lower()   # sim | dry_run | live
SIM_STARTING_EQUITY = float(os.getenv("SIM_STARTING_EQUITY", "30000"))
SIM_QUOTE_CACHE_SEC = 10
SIM_FILL_START = "09:30"
SIM_FILL_END = "16:00"
SIM_INDEX_FILL_END = "16:15"

# Only consulted in dry_run/live modes; kept False in sim mode so the simulator is selected.
DRY_RUN = SCHWAB_MODE != "sim" and \
    os.getenv("SCHWAB_LIVE_ORDERS", "false").strip().lower() not in ("1", "true", "yes")
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
