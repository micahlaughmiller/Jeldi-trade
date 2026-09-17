# ============================================
# 0DTE STRATEGY CONFIG - ASTRA & CLAUDE
# ORB + RSI Confluence for SPX 0DTE Options
# ============================================

import pytz
from datetime import time
from dataclasses import dataclass
import os
from dotenv import load_dotenv

# Point this directly to your token file location
TOKEN_PATH = os.path.abspath("token.json")  # or absolute path: "/path/to/token.json"

load_dotenv()

MARKET_TIMEZONE = pytz.timezone('US/Eastern')
MARKET_OPEN = time(9, 30)
START_TRADING = time(9, 0)
END_TRADING = time(12, 30)

# ============================================
# SCHWAB API CONFIGURATION (OAuth2)
# ============================================

# Paper trading: True | Live trading: False
PAPER_TRADING = True

# Get these from: https://developer.schwab.com
# DO NOT hardcode credentials - use .env file instead
SCHWAB_CLIENT_ID = os.getenv('SCHWAB_APP_KEY', '')
SCHWAB_SECRET_KEY = os.getenv('SCHWAB_APP_SECRET', '')
SCHWAB_BASE_URL = os.getenv("SCHWAB_BASE_URL", "https://api.schwabapi.com/trader/v1")
# Get the directory where config.py lives (C:\Users\Micah\Trading-Automation\schwab\0dte)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Read the path from .env, but convert it to an absolute path
env_token_path = os.getenv("SCHWAB_TOKEN_PATH", "./token.json")
if env_token_path.startswith("."):
    # Strip leading './' or '.' and resolve relative to BASE_DIR
    TOKEN_PATH = os.path.abspath(os.path.join(BASE_DIR, env_token_path))
else:
    TOKEN_PATH = os.path.abspath(env_token_path)

print(f"[DEBUG] Resolved TOKEN_PATH to: {TOKEN_PATH}")

LOG_FILE = "0dte_trading.log"
DEBUG_MODE = False

# ============================================
# TRADER CONFIGURATION
# ============================================

TRADER_NAME = 'MICAH'
ACCOUNT_NAME = 'Schwab Trading Account'

# ============================================
# ACCOUNT & RISK PARAMETERS
# ============================================

INITIAL_ACCOUNT_EQUITY = 10000.00
MAX_RISK_PER_TRADE_PCT = 0.05
MAX_PORTFOLIO_RISK_PCT = 0.52
MAX_CONTRACTS_PER_TRADE = 10

# ============================================
# POSITION SIZING TIERS
# ============================================

EFFECTIVE_MAX_EQUITY = 10000.00
POSITION_SIZE_TIERS = {
    10000: 3,
    25000: 5,
    50000: 10,
    100000: 15,
}

# ============================================
# OPTIONS PARAMETERS
# ============================================

RSI_PERIOD_FAST = 14
RSI_PERIOD_SLOW = 28
RSI_OVERSOLD_THRESHOLD = 30
RSI_OVERBOUGHT_THRESHOLD = 70

TARGET_DELTA = 0.30
SPREAD_WIDTH = 5.00
MIN_CREDIT_TARGET = 1.45
MIN_CREDIT_FLOOR = 1.35

# ============================================
# ORDER MANAGEMENT
# ============================================

PRICE_REDUCTION_INTERVAL_HOURS = 0.5
PRICE_REDUCTION_AMOUNT = 0.02
ORDER_TYPE = "limit"
TIME_IN_FORCE = "gtc"

# ============================================
# EXIT PARAMETERS
# ============================================

PROFIT_TARGET_PCT = 0.50
EXPIRATION_DTE_MIN = 0
EXPIRATION_DTE_MAX = 1

# ============================================
# CIRCUIT BREAKERS
# ============================================

MAX_LOSS_HITS_CIRCUIT_BREAKER = 3
DAILY_LOSS_LIMIT_PCT = 0.10
PEAK_DRAWDOWN_LIMIT_PCT = 0.20

# ============================================
# STRATEGY CONFIG
# ============================================

@dataclass(frozen=True)
class StrategyConfig:
    orb_minutes: int = 15
    confirmation_candles: int = 2
    min_credit: float = 1.45
    preferred_credit: float = 2.00
    max_credit: float = 3.00
    stop_amount: float = 0.30
    profit_trigger: float = 0.30
    risk_per_trade: float = 0.05
    max_daily_loss_percent: float = 0.10
    max_consecutive_losses: int = 2
    max_trades_per_day: int = 5
    short_strike_distance: float = 10.0
    absolute_max_contracts: int = 10
    absolute_max_spread_width: int = 10
    paper_only: bool = True

CONFIG = StrategyConfig()
DATA_SOURCE = "yfinance"
LOGGING_DIR = f"./{TRADER_NAME.lower()}_logs_0dte"