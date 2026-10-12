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
the broker section differs -- except the exit rules (EXIT_DTE_ENABLED, MAX_LOSS_EXIT,
PROFIT_FLOOR_MODE/_ARM_PCT_OF_TARGET/_TRAIL_TICK), which are deliberately Schwab-only
and are allow-listed in tools/check_sync.py. Run tools/check_sync.py to confirm.
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
# 2026-10-08: adopt() takes over an untracked option spread only when it is on a universe name AND expires more than this
# many days out. The account may be shared with the 0DTE bot (SPXW, and its stock-ORB "F" spreads on 0-7 DTE expirations
# of the same big names): adopting those would put them under this bot's DTE<=EXIT_DTE exit and close them. Positions
# already in state.json are always kept, whatever their DTE.
ADOPT_MIN_DTE = 7
# 2026-10-02, Schwab only (the user's call after a position was stopped out at max loss with plenty of
# time left to recover): the ONLY exits are (1) the GTC close at PROFIT_TARGET_PCT of max profit and
# (2) the tick trail below. No DTE force-close and no max-loss stop -- a position that is under water
# rides to the profit target or to expiration. The circuit breaker counts only REALIZED max losses
# (a close at >= MAX_LOSS_HIT_PCT of max loss, or an expiry whose last mark was at/beyond that line);
# a mark that dips there and recovers does not count (new entries halt after
# MAX_LOSS_HITS_CIRCUIT_BREAKER realized ones). Alpaca/45DTE keeps the original rules (EXIT_DTE_ENABLED/MAX_LOSS_EXIT on, fixed floor).
EXIT_DTE_ENABLED = False
MAX_LOSS_EXIT = False
# Profit floor, "trail" mode: once a position gets PROFIT_FLOOR_ARM_PCT_OF_TARGET (40%) of the way to
# the profit target (40% of a 50%-of-max target = 20% of max profit; entry 1.60 arms at 1.30), the next
# PROFIT_FLOOR_TRAIL_TICK (5 cents) give-back from the best price seen exits at market for whatever
# profit that is. If the price reverses through entry before that exit can fire (unrealized P/L not
# positive), nothing is sold: the floor disarms and the position rides, re-arming if it gets back to
# the arm level. (Original 2026-09-25 background: a real trade reached 0.48 on a 0.80 entry, then
# reversed through entry with no giveback protection at all.)
PROFIT_FLOOR_ENABLED = True
PROFIT_FLOOR_MODE = "trail"
PROFIT_FLOOR_TRAIL_TICK = 0.05
# The trail only sells when the mid price still shows at least this much profit (a market close fills
# at the natural price, up to a half-spread worse than the mid); otherwise it disarms and rides.
PROFIT_FLOOR_MIN_PROFIT = 0.05
PROFIT_FLOOR_ARM_PCT_OF_TARGET = 0.40
PROFIT_FLOOR_PCT_OF_MAX = 0.30   # only used by PROFIT_FLOOR_MODE "fixed"
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
