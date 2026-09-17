# 45-60 DTE Credit Spread System Configuration

# ============================================================================
# ALPACA PAPER TRADING
# ============================================================================
PAPER_TRADING = True
ALPACA_API_KEY = "PKCYKDC5VQSUVMTMH3ZHWHUQDZ"
ALPACA_SECRET_KEY = "JAPzzh9UnTKdSPhoQiGVr3zqozGNFHy9myMW6o6ZJfb"
ALPACA_BASE_URL = "https://paper-api.alpaca.markets" if PAPER_TRADING else "https://api.alpaca.markets"

# ============================================================================
# ACCOUNT & RISK PARAMETERS
# ============================================================================
INITIAL_ACCOUNT_EQUITY = 5000.00
MAX_RISK_PER_TRADE_PCT = 0.04  # 4% of account per trade
MAX_PORTFOLIO_RISK_PCT = 0.52  # 52% maximum exposure across all trades
MAX_CONTRACTS_PER_TRADE = 40

# ============================================================================
# POSITION SIZING TIERS (for account growth strategy)
# ============================================================================
EFFECTIVE_MAX_EQUITY = 10000.00  # Treat account as if it has this much for sizing
POSITION_SIZE_TIERS = {
    10000: 3,      # Up to $10k: max 3 contracts
    25000: 5,      # Up to $25k: max 5 contracts
    50000: 10,     # Up to $50k: max 10 contracts
    100000: 15,    # Up to $100k: max 15 contracts
}  # Tiers are evaluated in order - uses first tier where account_size <= tier_limit

# ============================================================================
# SIGNAL & ENTRY PARAMETERS
# ============================================================================
# RSI Indicators
RSI_PERIOD_FAST = 14
RSI_PERIOD_SLOW = 28
RSI_OVERSOLD_THRESHOLD = 30
RSI_OVERBOUGHT_THRESHOLD = 70

# Timeframe
DATA_LOOKBACK_MONTHS = 6  # 6-month chart
CANDLE_TYPE = "1D"  # Daily candles

# Entry Requirements
TARGET_DELTA = 0.30  # Short strike delta
SPREAD_WIDTH = 5.00  # $5 wide spread
MIN_CREDIT_TARGET = 1.45  # Minimum credit to enter ($1.45)
MIN_CREDIT_FLOOR = 1.35  # Minimum credit before canceling ($1.35)

# ============================================================================
# EXIT PARAMETERS
# ============================================================================
PROFIT_TARGET_PCT = 0.50  # Close at 50% of max profit
EXPIRATION_DTE_MIN = 45
EXPIRATION_DTE_MAX = 60

# ============================================================================
# ORDER MANAGEMENT
# ============================================================================
PRICE_REDUCTION_INTERVAL_HOURS = 1  # Reduce price every 1 hour
PRICE_REDUCTION_AMOUNT = 0.02  # Reduce by $0.02 per interval
ORDER_TYPE = "limit"
TIME_IN_FORCE = "gtc"  # Good-Till-Cancelled for profit targets

# ============================================================================
# CIRCUIT BREAKERS & SAFETY
# ============================================================================
MAX_LOSS_HITS_CIRCUIT_BREAKER = 3  # If 3+ trades hit max loss, halt new entries
DAILY_LOSS_LIMIT_PCT = 0.03  # 3% daily loss triggers alert
PEAK_DRAWDOWN_LIMIT_PCT = 0.15  # 15% peak-to-trough triggers freeze

# ============================================================================
# SP500 SCAN
# ============================================================================
SP500_SCAN_TIME = "09:35"  # Scan 5 min after market open (ET)
SP500_SCAN_WINDOW_SECONDS = 300  # 5-minute window to trigger scan
CHECK_EXISTING_POSITIONS = True  # Skip tickers already in position

# ============================================================================
# LOGGING
# ============================================================================
LOG_DIR = "./logs_45dte"
LOG_LEVEL = "INFO"
ENABLE_DAILY_SUMMARY = True
