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
OPENING_RANGE_MINUTES = 30
OVERNIGHT_ENTRY_END_MIN = 30
LAST_ENTRY_TIME = time(15, 0)     # 2:00 pm CST: last new entry
FORCE_CLOSE_TIME = time(15, 30)   # 2:30 pm CST: everything closed

TICK_SECONDS = 15
CANDLE_INTERVAL = "2m"
ORB_CANDLE_INTERVAL = "5m"
ORB_SETUP_TIMEOUT_CANDLES = 6
CANDLE_LOOKBACK_MIN = 180
DATA_CACHE_SEC = 20

BREAKOUT_LEVEL_SOURCE = "ES_TO_SPX"

# Every signal opens two independent spreads: A sells the ITM spread, B sells an OTM spread
# whose short strike sits one expected move (ATM straddle) away from spot.
STRATEGIES = ("A", "B")
STRATEGY_LABELS = {"A": "A - ITM", "B": "B - OTM (expected move)"}

WIDTH_BY_TIER = {1: 5, 2: 5, 3: 10, 4: 10}
CREDIT_RANGE_BY_WIDTH = {5: (2.75, 3.50), 10: (5.50, 7.00)}
B_CREDIT_RANGE_BY_WIDTH = {5: (0.65, 1.10), 10: (1.30, 2.20)}
B_MIN_DISTANCE_FROM_SPOT = 10
# Strategy A strike bias. A mid above this is deeper ITM than we want (the 6.50-7.00 entries were
# the worst bucket on 2026-09-21): walk both strikes one strike toward spot, up to A_MAX_STRIKE_WALK
# times, keeping the short strike ITM, instead of entering at the top of the band or rejecting
# CREDIT_ABOVE_MAX. The exit triggers (mid vs target/stop) are unchanged.
A_CREDIT_BIAS_BY_WIDTH = {5: 3.25, 10: 6.50}
A_MAX_STRIKE_WALK = 2
TIER_BANDS = [(10_000, 1), (30_000, 2), (50_000, 3), (float("inf"), 4)]
RISK_PER_TRADE_PCT = 0.05
ALLOW_MIN_CONTRACT_OVERRIDE = True
MAX_PORTFOLIO_RISK_PCT = 0.52
MAX_CONTRACTS_PER_TRADE = 10
# Trade count and loss streak are per strategy; the daily loss limit and portfolio risk cap are combined.
MAX_TRADES_PER_DAY = 20
MAX_CONSECUTIVE_LOSSES = 5
# Per-strategy overrides of the two counters plus a cool-down (minutes after that strategy's last exit
# before it may enter again). 2026-09-21: 11-16 A trades per persona re-bought the same breakout every
# five minutes; capping at 3 trades would have cut the day's loss by 84%. B keeps the defaults.
LIMITS_BY_STRATEGY = {
    "A": {"MAX_TRADES_PER_DAY": 5, "MAX_CONSECUTIVE_LOSSES": 5, "COOLDOWN_MIN": 30},   # 2026-09-22: 5 trades to test the exit floors
    "B": {},
}
DAILY_LOSS_LIMIT_PCT = 0.10
MAX_CONCURRENT_POSITIONS = 1   # per strategy

# Which setups and which ORB entry kinds each strategy trades. 2026-09-21: entries before 10:00 went
# 1 for 12 and momentum-close entries bought the top of the 5-minute bar, so A takes only the ORB
# pullback entry (wick to the level, then a close in the breakout direction). B is unchanged.
SETUPS_BY_STRATEGY = {"A": ("ORB",), "B": ("OVERNIGHT", "ORB")}
# Both entry kinds for A: the 2026-09-21 replay showed a trend day with one momentum break and no pullback.
ORB_ENTRY_KINDS_BY_STRATEGY = {"A": ("MOMENTUM", "PULLBACK"), "B": ("MOMENTUM", "PULLBACK")}

PROFIT_TARGET = 0.30
STOP_LOSS = 0.55
B_PROFIT_TARGET = 0.30
B_STOP_LOSS = 0.50
RUNNER_ENABLED = True
RUNNER_MIN_CONTRACTS = 2
# Fraction of the position booked at the target when momentum continues; the rest runs with the stop at
# the target level and the trail. A runs the whole position (the trail was the only profit source on
# 2026-09-21: +$2,650 from six exits vs +$180 from 34 fixed-target exits); B books half as before.
RUNNER_CLOSE_FRACTION_BY_STRATEGY = {"A": 0.0, "B": 0.5}
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
RUNNER_MOMENTUM_GATE = True      # momentum must be continuing at the target for the runner to start
RUNNER_SLOWDOWN_EXIT = True      # runner exits when 3-candle momentum drops MOMENTUM_SLOWDOWN_PCT

# Per-strategy overrides of the exit knobs above (strategy.exit_setting). A, from the 2026-09-21 replay:
# lock arms at +0.30 and tolerates a 0.35 giveback (about one quote width), no candle-against exit, the
# runner always starts at the target, and no momentum-slowdown exit. That set turned the day's real entries
# from -$16.9k to +$13.5k on the model; the shared 0.15/0.10 lock cut every winner at about +0.10. B keeps
# the shared defaults until it has its own data.
# Profit floor: once the trade is `arm` in profit, a hard exit floor sits at `floor` profit while the position
# (and any runner) keeps going. Keyed by spread width: {width: (arm, floor)}. Empty = off.
PROFIT_FLOOR_BY_WIDTH: dict = {}
# Stale timer: STALE_TIMER_MIN minutes after entry, if the target has not been hit and the trade shows any
# profit, a floor at STALE_FLOOR profit is set (2026-09-22: trades that made +0.20 gave it all back to a loss).
STALE_TIMER_MIN = None
STALE_FLOOR = 0.05

A_BASE_TUNING = {"PROFIT_LOCK_ARM": 0.30, "PROFIT_LOCK_GIVEBACK": 0.35, "PROFIT_LOCK_ON_MOMENTUM_FLIP": False,
                 "RUNNER_MOMENTUM_GATE": False, "RUNNER_SLOWDOWN_EXIT": False}
EXIT_TUNING_BY_STRATEGY = {"A": dict(A_BASE_TUNING), "B": {}}

ENTRY_STEP_SEC = 20
ENTRY_PRICE_STEP = 0.05
ENTRY_TIMEOUT_SEC = 120

# Ctrl+C / SIGTERM / console close: close every open spread before exiting. 2026-09-21: five spreads
# left open across a 16-minute restart were adopted 1.50 underwater and cost $4,610.
CLOSE_ON_INTERRUPT = True

NEWS_DAYS = [
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
]
NEWS_DAY_MODE = "half_size"

# ------------------------------------------------------------------ persona experiments
# Nine personas firing on the same signal are one experiment run nine times. Give each one a different
# parameter set here and every session yields nine comparisons. Keys are any UPPERCASE name defined
# above; the dict for ACTIVE_TRADER is applied on import, everyone else runs the defaults.
# 2026-09-22 exit-floor experiment (Strategy A only; B untouched). ASTRA = control.
#   method 1  bracket at the first step, runner keeps going: $10-wide arms at +0.20 with a +0.15 floor,
#             $5-wide arms at +0.10 with a +0.05 floor
#   method 2  break-even lock: arms at +0.10, floor +0.05, both widths
#   method 3  5-minute stale timer: no target yet but in profit -> floor at +0.05
def _a(**extra) -> dict:
    return {"EXIT_TUNING_BY_STRATEGY": {"A": {**A_BASE_TUNING, **extra}, "B": {}}}


PERSONA_OVERRIDES: dict[str, dict] = {
    "ARIA":   _a(PROFIT_FLOOR_BY_WIDTH={10: (0.20, 0.15), 5: (0.10, 0.05)}),     # method 1, $10-wide
    "DAVID":  _a(PROFIT_FLOOR_BY_WIDTH={10: (0.20, 0.15), 5: (0.10, 0.05)}),     # method 1, $10-wide
    "SARAH":  _a(PROFIT_FLOOR_BY_WIDTH={10: (0.20, 0.15), 5: (0.10, 0.05)}),     # method 1, $5-wide
    "ELENA":  _a(PROFIT_FLOOR_BY_WIDTH={10: (0.10, 0.05), 5: (0.10, 0.05)}),     # method 2, $10-wide
    "JAMES":  _a(PROFIT_FLOOR_BY_WIDTH={10: (0.10, 0.05), 5: (0.10, 0.05)}),     # method 2, $5-wide
    "JORDAN": _a(STALE_TIMER_MIN=5),                                             # method 3, $10-wide
    "MARCUS": _a(STALE_TIMER_MIN=5),                                             # method 3, $5-wide
    "CLAUDE": _a(STALE_TIMER_MIN=5),                                             # method 3, $5-wide
}
globals().update(PERSONA_OVERRIDES.get(ACTIVE_TRADER, {}))
