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
# 2026-09-23: SPX 0DTE quotes are tight enough to work a closer limit; escalation step now $0.03/retry
# instead of $0.05 (close_spread_at_market: limit = ask-side + CLOSE_SLIPPAGE * attempt_number).
CLOSE_SLIPPAGE = 0.03
CLOSE_RETRY_SEC = 15
CLOSE_MAX_RETRIES = 6
LOG_DIR = "logs"

UNDERLYING = "SPX"
OPTION_ROOT = "SPXW"
SPX_SYMBOL = "^GSPC"
ES_SYMBOL = "ES=F"
# 2026-09-25: Schwab's own futures quote symbol for the same instrument -- this folder already
# authenticates to Schwab, so live ES via market_data.get_es_candles_live works here directly (no
# cross-broker credentials needed, unlike the Alpaca folder's copy of this same mechanism).
ES_SCHWAB_SYMBOL = "/ES"

MARKET_OPEN = time(9, 30)
OVERNIGHT_SESSION_START = time(18, 0)
OPENING_RANGE_MINUTES = 30
OVERNIGHT_ENTRY_END_MIN = 30
LAST_ENTRY_TIME = time(15, 0)     # 2:00 pm CST: last new entry
FORCE_CLOSE_TIME = time(15, 30)   # 2:30 pm CST: everything closed

# 2026-09-25: "as fast as Alpaca will let them" -- tightened from 15s. Watch for rate limiting
# across concurrent processes (options-chain calls in manage() are the heaviest; yfinance/Schwab
# candle calls are cache-protected at DATA_CACHE_SEC regardless of tick rate). Back this off if
# rate limiting shows up in the logs.
TICK_SECONDS = 1
CANDLE_INTERVAL = "2m"
ORB_CANDLE_INTERVAL = "5m"
ORB_SETUP_TIMEOUT_CANDLES = 6
CANDLE_LOOKBACK_MIN = 180
DATA_CACHE_SEC = 20

# ES_TO_SPX: ES overnight levels + (SPX - ES) basis, compared against live SPX candles -- or, once
# market_data.live_es_available() is true (this folder's own Schwab auth already covers it), ES's
# own candles directly for the real lead-time edge (scheduler.overnight_levels_for_signal).
# ES: compare ES candles directly against ES levels (Yahoo ES is ~10 min delayed) unconditionally.
BREAKOUT_LEVEL_SOURCE = "ES_TO_SPX"

# Every signal opens two independent spreads: A sells the ITM spread, B sells an OTM spread
# whose short strike sits one expected move (ATM straddle) away from spot.
# 2026-09-25: STRATEGIES stays the default two (A/B); ALL_STRATEGIES is the full catalog.
STRATEGIES = ("A", "B")
ALL_STRATEGIES = ("A", "B", "C", "D", "E")
STRATEGY_LABELS = {
    "A": "A - ITM", "B": "B - OTM (expected move)",
    "C": "C - ES overnight-break (OTM, same as B)",
    "D": "D - MA/Bollinger reversion, trend-gated",
    "E": "E - MA/Bollinger reversion, 5-min chop confirm",
}

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
    # 2026-09-25: 5 trades/day per strategy for the new multi-strategy personas (each active
    # strategy independently gets its own 5/day allowance, not a combined cap).
    "C": {"MAX_TRADES_PER_DAY": 5},
    "D": {"MAX_TRADES_PER_DAY": 5},
    "E": {"MAX_TRADES_PER_DAY": 5},
}
DAILY_LOSS_LIMIT_PCT = 0.10
MAX_CONCURRENT_POSITIONS = 1   # per strategy

# Which setups and which ORB entry kinds each strategy trades. 2026-09-21: entries before 10:00 went
# 1 for 12 and momentum-close entries bought the top of the 5-minute bar, so A takes only the ORB
# pullback entry (wick to the level, then a close in the breakout direction). B is unchanged.
# ON_BREAK: the overnight high/low run through the same break -> pullback/momentum state machine as the
# ORB, on 2-minute candles during the first 30 minutes. A trades it; B keeps its own OVERNIGHT detector.
# 2026-09-24: B now trades ON_BREAK (the same single-shot break/pullback/momentum state machine A
# uses) instead of the old OVERNIGHT detector, which had no re-arm gate and could re-fire on every
# candle of a continuing move (32 B entries off one signal on 2026-09-23). OVERNIGHT is retired for
# both strategies; the detector function itself (strategy.detect_breakout) is left in place, unused.
# ES_BREAK (2026-09-25, Strategy C): the SAME overnight level run through strategy.TwoCandleBreak
# instead of OrbSetup -- a deliberately simpler close-only break+confirm ("no wicks count") rather
# than ON_BREAK's wick-aware break/pullback/momentum machine. D and E are NOT setup-driven: their
# signal comes from check_mean_reversion_entry each tick, not from a fired breakout.
SETUPS_BY_STRATEGY = {"A": ("ORB", "ON_BREAK"), "B": ("ON_BREAK", "ORB"), "C": ("ES_BREAK",)}
# Both entry kinds for A: the 2026-09-21 replay showed a trend day with one momentum break and no pullback.
ORB_ENTRY_KINDS_BY_STRATEGY = {"A": ("MOMENTUM", "PULLBACK"), "B": ("MOMENTUM", "PULLBACK")}

PROFIT_TARGET = 0.30
STOP_LOSS = 0.60
# Ticks the spread price must sit at/above the stop before it actually closes (default 1 = immediate,
# today's behavior everywhere). 2026-09-23: five personas hit an identical ON_BREAK entry; two were
# stopped out within the same minute by what looks like a single noisy print, while the other three
# rode the same move to a runner win. A requires 2 consecutive ticks (~30s) to confirm a stop.
STOP_CONFIRM_TICKS = 1
B_PROFIT_TARGET = 0.30
B_STOP_LOSS = 0.50
# C, D: same target/stop as B by default. E: 5-cent target for the choppy-day variant; its stop
# defaults to B's pending real tuning.
C_PROFIT_TARGET = B_PROFIT_TARGET
C_STOP_LOSS = B_STOP_LOSS
D_PROFIT_TARGET = B_PROFIT_TARGET
D_STOP_LOSS = B_STOP_LOSS
E_PROFIT_TARGET = 0.05
E_STOP_LOSS = B_STOP_LOSS
RUNNER_ENABLED = True
RUNNER_MIN_CONTRACTS = 2
# Fraction of the position booked at the target when momentum continues; the rest runs with the stop at
# the target level and the trail. A runs the whole position (the trail was the only profit source on
# 2026-09-21: +$2,650 from six exits vs +$180 from 34 fixed-target exits); B books half as before.
RUNNER_CLOSE_FRACTION_BY_STRATEGY = {"A": 0.0, "B": 0.5}
TRAIL_AMOUNT = 0.50
# Once a runner's best profit passes RUNNER_DEEP_PROFIT, tighten the trail to RUNNER_DEEP_TRAIL so a
# pullback is caught closer to +0.30 instead of riding the flat $0.50 trail all the way down (2026-09-23:
# a runner gave back $0.80 before the wider trail + escalating-limit fill caught it). Off by default;
# strategy A turns it on via EXIT_TUNING_BY_STRATEGY (RUNNER_TIGHTEN_ENABLED).
# 2026-09-23: today's A runners peaked between $1.35 and $2.05 before pulling back -- all below the old
# $2.00 threshold, so the flat $0.50 trail (plus close-order slippage) gave back up to $0.67. Lowered to
# $1.00 so the tighter trail engages on runners like today's instead of only on very deep ones.
RUNNER_DEEP_PROFIT = 1.00
RUNNER_DEEP_TRAIL = 0.30
RUNNER_TIGHTEN_ENABLED = False

# Once a MOMENTUM entry_kind signal fires, a strategy with MOMENTUM_CONFIRM_ENABLED does not enter
# immediately -- it waits for MOMENTUM_CONFIRM_CANDLES consecutive completed 1-minute SPX candles,
# each closing at least MOMENTUM_CONFIRM_MARGIN points beyond the break level in the trade direction,
# before actually entering (a fresh spot/chain is fetched at that later time, same as any entry).
# A candle that fails the margin aborts the pending entry; MOMENTUM_CONFIRM_TIMEOUT_MIN gives up if
# confirmation never resolves either way. PULLBACK entries are unaffected -- their own wick-then-
# reconfirm pattern already requires this kind of follow-through. Pending state is in-memory only
# and does not survive a restart. 2026-09-24: a 0.18-point margin was enough to trigger MOMENTUM and
# enter right at the exhaustion of the morning's move.
MOMENTUM_CONFIRM_ENABLED = False
MOMENTUM_CONFIRM_MARGIN = 0.50
MOMENTUM_CONFIRM_CANDLES = 2
MOMENTUM_CONFIRM_TIMEOUT_MIN = 5

# Once a setup's breakout has gone DONE (fired, hasn't reset) and a strategy with
# REENTRY_ON_CONTINUATION_ENABLED is flat, it tries a fresh entry every REENTRY_PAUSE_MIN minutes for
# as long as spot keeps confirming the move (still beats the break level by MOMENTUM_CONFIRM_MARGIN)
# -- unlike the old OVERNIGHT detector (retired 2026-09-24), which re-fired on every completed candle
# regardless of whether the move had stalled (32 entries off one signal on 2026-09-23), this only
# re-enters on a fixed cadence and only while price is still actually confirming continuation.
REENTRY_ON_CONTINUATION_ENABLED = False
REENTRY_PAUSE_MIN = 5

# Day-type/regime gate: before a strategy with DAY_GATE_ENABLED takes ANY entry, the session's own
# Kaufman efficiency ratio (net move / total path length on 2-min SPX candles since 09:30, recomputed
# fresh at every entry attempt, not just once at the open) must clear DAY_GATE_MIN_ER, with at least
# DAY_GATE_MIN_CANDLES of session data to trust the reading yet. A choppy/inefficient reading skips
# that entry entirely -- it never touches the stop, target or any other exit parameter, deliberately:
# a live regime read feeding into stop width would break the assumption every other strategy relies on
# that exit_levels(strat) is a fixed pair for the day. Off by default for both strategies; a persona
# override turns it on for a live test.
DAY_GATE_ENABLED = False
DAY_GATE_MIN_ER = 0.15
DAY_GATE_MIN_CANDLES = 5
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
                 "RUNNER_MOMENTUM_GATE": False, "RUNNER_SLOWDOWN_EXIT": False, "RUNNER_TIGHTEN_ENABLED": True,
                 "STOP_CONFIRM_TICKS": 2, "MOMENTUM_CONFIRM_ENABLED": True}
B_BASE_TUNING = {"REENTRY_ON_CONTINUATION_ENABLED": True}
C_BASE_TUNING: dict = {}
D_BASE_TUNING: dict = {}
E_BASE_TUNING: dict = {}
EXIT_TUNING_BY_STRATEGY = {
    "A": dict(A_BASE_TUNING), "B": dict(B_BASE_TUNING),
    "C": dict(C_BASE_TUNING), "D": dict(D_BASE_TUNING), "E": dict(E_BASE_TUNING),
}

ENTRY_STEP_SEC = 20
ENTRY_PRICE_STEP = 0.05
ENTRY_TIMEOUT_SEC = 120

# Ctrl+C / SIGTERM / console close: close every open spread before exiting. 2026-09-21: five spreads
# left open across a 16-minute restart were adopted 1.50 underwater and cost $4,610.
CLOSE_ON_INTERRUPT = True

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

# 2026-09-25: bot-wide news blackout (all strategies) -- no new entries from NEWS_BLACKOUT_BEFORE_MIN
# before to NEWS_BLACKOUT_AFTER_MIN after any time listed for today in NEWS_EVENTS, sourced from
# MarketWatch/Yahoo Finance calendars. Keyed by ISO date since real calendar events move month to
# month. Common recurring ET slots for reference (NOT pre-populated):
#   08:30  many BLS/Census releases (CPI, PPI, jobs report, jobless claims, retail sales)
#   10:00  ISM, consumer confidence/sentiment
#   14:00  FOMC statement
NEWS_EVENTS: dict[str, list[time]] = {}
NEWS_BLACKOUT_BEFORE_MIN = 5
NEWS_BLACKOUT_AFTER_MIN = 5

# ------------------------------------------------------------ Strategies D/E: MA/Bollinger reversion
# Intraday adaptation of the dictated EMA(9)/EMA(30)+Bollinger(20,2) secondary strategy, using the
# indicators as taught in https://youtu.be/3uqr_tf8fr8 (Invest with Henry): a single 30-period line
# for trend/timing plus a Bollinger Band matched to that SAME period, per the user's explicit "match
# the BB and moving average to 30 days" instruction. The 9 EMA classifies the day rising/falling/choppy.
D_EMA_FAST_SPAN = 9
D_EMA_SLOW_SPAN = 30          # also the Bollinger basis
D_BB_PERIOD = 30
D_BB_STD = 2.0
# "Crossing" (9 EMA vs 30 EMA within this many SPX points) reads as flat/choppy -- sit out entirely.
# Not given an exact number by the user -- a starting assumption pending real tuning.
D_EMA_CHOP_THRESHOLD_PTS = 2.0
# "90% away from middle" on the standard 0-1 %B scale: >= 0.95 (upper) or <= 0.05 (lower).
D_BAND_TOUCH_PCT_B = 0.95
# Candle interval D/E compute their EMA/Bollinger read on, and E's two-candle chop confirmation uses.
D_E_CANDLE_INTERVAL = "5m"
D_E_MIN_CANDLES = 30           # need a full period before the EMA(30)/Bollinger(30) read is trustworthy
E_CONFIRM_CANDLES = 2

# ---------------------------------------------------------------- testing-phase contract sizing
# 2026-09-25: while C/D/E are being live-tested, contract size is capped well below the normal
# tier-based sizing -- half that cap again for Strategy D (always) and Strategy E (choppy-day
# variant). Supersedes MAX_CONTRACTS_PER_TRADE while TESTING_MODE is True; revert once testing ends.
TESTING_MODE = True
TESTING_MAX_CONTRACTS = 6
TESTING_HALF_SIZE_MAX_CONTRACTS = 3
