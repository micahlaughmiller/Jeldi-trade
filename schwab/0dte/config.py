"""0DTE SPX credit-spread bot configuration (Schwab).

Secrets come from the folder's .env (python-dotenv); this file holds no keys.
All times are US/Eastern wall-clock.

Schwab has NO paper-trading API: every submitted order is real money. SCHWAB_MODE
picks how orders are handled: `sim` (default) trades a local simulated account
filled against live Schwab quotes (paper_sim.py), `dry_run` only logs order
payloads, and `live` sends real orders -- but only when SCHWAB_LIVE_ORDERS=true is
also set in .env; otherwise live is forced back to dry-run.
Strategy parameters below are identical to Alpaca/0DTE/config.py; only the
broker section differs. Run tools/check_sync.py to confirm.
"""

import os
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

ET = ZoneInfo("US/Eastern")

TRADER_NAME = os.getenv("TRADER_NAME", "SCHWAB").upper()

SCHWAB_APP_KEY = os.getenv("SCHWAB_APP_KEY", "")
SCHWAB_APP_SECRET = os.getenv("SCHWAB_APP_SECRET", "")
SCHWAB_CALLBACK_URL = os.getenv("SCHWAB_CALLBACK_URL", "https://127.0.0.1:8080")
SCHWAB_TOKEN_PATH = os.getenv("SCHWAB_TOKEN_PATH", str(Path(__file__).resolve().parent / "token.json"))
SCHWAB_ACCOUNT_INDEX = int(os.getenv("SCHWAB_ACCOUNT_INDEX", "0"))
PAPER_TRADING = False

# Optional. Schwab serves no option chain for $SPX on some accounts; with Alpaca keys
# present, SPX/SPXW quotes and expirations are read from Alpaca's indicative feed instead.
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
ALPACA_DATA_URL = os.getenv("ALPACA_DATA_URL", "https://data.alpaca.markets")

SCHWAB_MODE = os.getenv("SCHWAB_MODE", "sim").strip().lower()   # sim | dry_run | live
SIM_STARTING_EQUITY = float(os.getenv("SIM_STARTING_EQUITY", "2000"))
SIM_QUOTE_CACHE_SEC = 10
SIM_FILL_START = "09:30"
SIM_FILL_END = "16:00"
SIM_INDEX_FILL_END = "16:15"

# scheduler.py passes `--dry-run or DRY_RUN` to Broker(), and dry_run=True always selects the
# log-only broker, so DRY_RUN must be False in sim mode for the simulator to be chosen.
DRY_RUN = SCHWAB_MODE != "sim" and \
    os.getenv("SCHWAB_LIVE_ORDERS", "false").strip().lower() not in ("1", "true", "yes")
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
OPENING_RANGE_MINUTES = 30
OVERNIGHT_ENTRY_END_MIN = 30
LAST_ENTRY_TIME = time(12, 0)
FORCE_CLOSE_TIME = time(12, 30)

TICK_SECONDS = 15
CANDLE_INTERVAL = "2m"
ORB_CANDLE_INTERVAL = "5m"
ORB_SETUP_TIMEOUT_CANDLES = 6
CANDLE_LOOKBACK_MIN = 180
DATA_CACHE_SEC = 20

# ES_TO_SPX: ES overnight levels + (SPX - ES) basis, compared against live SPX candles.
# ES: compare ES candles directly against ES levels (Yahoo ES is ~10 min delayed).
BREAKOUT_LEVEL_SOURCE = "ES_TO_SPX"

# Every signal opens two independent spreads: A sells the ITM spread, B sells an OTM spread
# whose short strike sits one expected move (ATM straddle) away from spot.
STRATEGIES = ("A", "B")
STRATEGY_LABELS = {"A": "A - ITM", "B": "B - OTM (expected move)"}

WIDTH_BY_TIER = {1: 5, 2: 5, 3: 10, 4: 10}
CREDIT_RANGE_BY_WIDTH = {5: (2.75, 3.50), 10: (5.50, 7.00)}
B_CREDIT_RANGE_BY_WIDTH = {5: (0.65, 1.10), 10: (1.30, 2.20)}
B_MIN_DISTANCE_FROM_SPOT = 10

TIER_BANDS = [(10_000, 1), (30_000, 2), (50_000, 3), (float("inf"), 4)]
RISK_PER_TRADE_PCT = 0.05
ALLOW_MIN_CONTRACT_OVERRIDE = True
MAX_PORTFOLIO_RISK_PCT = 0.52
MAX_CONTRACTS_PER_TRADE = 10
# Trade count and loss streak are per strategy; the daily loss limit and portfolio risk cap are combined.
MAX_TRADES_PER_DAY = 20
MAX_CONSECUTIVE_LOSSES = 5
DAILY_LOSS_LIMIT_PCT = 0.10
MAX_CONCURRENT_POSITIONS = 1   # per strategy

PROFIT_TARGET = 0.30
STOP_LOSS = 0.30
B_PROFIT_TARGET = 0.30
B_STOP_LOSS = 0.50
RUNNER_ENABLED = True
RUNNER_MIN_CONTRACTS = 2
TRAIL_AMOUNT = 0.50
MOMENTUM_CANDLES = 3
MOMENTUM_CONTINUE_RATIO = 0.80
MOMENTUM_SLOWDOWN_PCT = 0.20

# Lock in an open profit before the target: once the spread has moved PROFIT_LOCK_ARM in our
# favor, exit if it gives back PROFIT_LOCK_GIVEBACK from its best level, or (optionally) if the
# last completed SPX candle closes against the trade.
PROFIT_LOCK_ENABLED = True
PROFIT_LOCK_ARM = 0.15
PROFIT_LOCK_GIVEBACK = 0.10
PROFIT_LOCK_ON_MOMENTUM_FLIP = True

ENTRY_STEP_SEC = 20
ENTRY_PRICE_STEP = 0.05
ENTRY_TIMEOUT_SEC = 120

# 2026 FOMC decision dates -- verify against federalreserve.gov
NEWS_DAYS = [
    "2026-01-28",
    "2026-03-18",
    "2026-04-29",
    "2026-06-17",
    "2026-07-29",
    "2026-09-16",
    "2026-10-28",
    "2026-12-09",
]
NEWS_DAY_MODE = "half_size"
