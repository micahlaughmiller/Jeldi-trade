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
def _token_file(raw: str, default: str) -> str:
    """The token file to use. A blank setting means the default; a setting that names an existing FOLDER (the Schwab bot's own folder)
    means the token.json inside it -- opening a folder as a file raises PermissionError on Windows, which once looked like a locked file."""
    chosen = Path((raw or "").strip() or default)
    if not chosen.is_absolute():                      # 2026-10-26: relative = relative to THIS folder, whatever the working directory is
        chosen = (Path(__file__).resolve().parent / chosen).resolve()
    return str(chosen / "token.json") if chosen.is_dir() else str(chosen)


SCHWAB_TOKEN_PATH = _token_file(os.getenv("SCHWAB_TOKEN_PATH", ""), str(Path(__file__).resolve().parent / "token.json"))
SCHWAB_ACCOUNT_INDEX = int(os.getenv("SCHWAB_ACCOUNT_INDEX", "0"))
PAPER_TRADING = False

# Optional. Schwab serves no option chain for $SPX on some accounts; with Alpaca keys
# present, SPX/SPXW quotes and expirations are read from Alpaca's indicative feed instead.
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
ALPACA_DATA_URL = os.getenv("ALPACA_DATA_URL", "https://data.alpaca.markets")

SCHWAB_MODE = os.getenv("SCHWAB_MODE", "sim").strip().lower() or "sim"   # sim | dry_run | live
if SCHWAB_MODE not in ("sim", "dry_run", "live"):          # 2026-10-15: a typo must never fall through to live
    raise ValueError(f"SCHWAB_MODE={SCHWAB_MODE!r} is not one of sim, dry_run, live (fix it in .env)")
SIM_STARTING_EQUITY = float(os.getenv("SIM_STARTING_EQUITY", "2000"))
# 2026-10-15 GO-LIVE SAFETY (live and dry_run modes; the simulator is never capped). The capital the bot sees is the GREATER of
# LIVE_CAPITAL_FLOOR_USD and LIVE_CAPITAL_PCT of the account's total value (never more than the account holds), plus the day's real P/L; opening orders
# are clamped to LIVE_MAX_CONTRACTS_PER_TRADE contracts, and the strategies' own sizing is capped by HARD_MAX_CONTRACTS. Live orders also need an account
# chosen with schwab_select_account.py after each login. Raise the contract cap only once the live model has been shaken out.


def _live_setting(name, default, cast, valid, rule):
    """A go-live limit from the environment. Blank = the default. An invalid value stops the bot at start-up (a typo such as 0, 25 or 'abc' must never
    switch a cap off or widen it); the simulator ignores these limits, so there an invalid value just falls back to the default."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
        if valid(value):
            return value
    except ValueError:
        pass
    if SCHWAB_MODE == "sim":
        return default
    raise ValueError(f"{name}={raw!r} is invalid ({rule}); fix it in .env")


LIVE_CAPITAL_CAP_ENABLED = SCHWAB_MODE != "sim"
LIVE_CAPITAL_FLOOR_USD = _live_setting("LIVE_CAPITAL_FLOOR_USD", 1000.0, float, lambda v: 0 <= v < 1e9, "dollars, 0 or more")
LIVE_CAPITAL_PCT = _live_setting("LIVE_CAPITAL_PCT", 0.25, float, lambda v: 0 < v <= 1, "a fraction above 0 and at most 1, e.g. 0.25")
LIVE_MAX_CONTRACTS_PER_TRADE = _live_setting("LIVE_MAX_CONTRACTS_PER_TRADE", 1, int, lambda v: v >= 1, "a whole number of at least 1")
HARD_MAX_CONTRACTS = LIVE_MAX_CONTRACTS_PER_TRADE if SCHWAB_MODE != "sim" else None
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
# 2026-11-03: SPX/SPXW orders are snapped DOWN to the $0.05 grid before they are sent (broker.snap_spx_price), so for SPX
# the first close limit is the ask-side itself and every retry climbs one $0.05 tick; stock options (F) keep this exact step.
CLOSE_SLIPPAGE = 0.03
# 2026-09-29: 15s -> 2s. A's profit-lock/stop exits were chasing a moving target during fast moves
# (one traced case: decision at 2.67, actual fill at 2.95 -- slippage well past the nominal
# CLOSE_SLIPPAGE*attempts escalation, because the broker re-quotes its ask-side fresh on each retry,
# so a slow retry cadence gives the underlying that much longer to keep running between attempts).
# Retrying every 2s instead of 15s means far less time for price to move between escalations.
# Note: CLOSE_MAX_RETRIES is unchanged, so the total give-up window also shrinks (90s -> 12s) --
# watch for MORE "MANUAL ACTION REQUIRED" gives-up events if 12s turns out to be too short in practice.
CLOSE_RETRY_SEC = 2
CLOSE_MAX_RETRIES = 6
# 2026-10-08: a close that failed on EVERY attempt (a rejection for permissions / buying power / day-trade protection, an
# outage) is latched: that position is not retried for these many seconds (the n-th failure waits the n-th value, the last
# one repeats), new entries are halted while any close is latched, and a CRITICAL "MANUAL ACTION REQUIRED" is logged. It used
# to be retried every tick (6 attempts, 18 HTTP calls) with no pause and no halt.
CLOSE_BLOCK_BACKOFF_SEC = (30, 60, 120, 300)
# At the hard stop, positions that still will not close keep the bot alive: it retries this often until they are flat or
# the session ends at MARKET_CLOSE (it used to give up after six passes and exit with the spread still open).
EOD_CLOSE_RETRY_SEC = 60
# 2026-10-08: data-failure safety. An open position whose price has not been refreshed for MARK_STALE_ALERT_SEC raises a
# CRITICAL alert and halts new entries; after MARK_STALE_FLATTEN_SEC it is closed at its last known price (reason
# STALE_DATA_FLATTEN) rather than left unmanaged -- a dead Schwab token, an Alpaca 429 burst or a Yahoo outage used to leave
# every stop and the hard stop switched off. F is priced only every F_PRICE_EVERY_SEC, so it gets longer thresholds.
STALE_MARK_FLATTEN_ENABLED = True
MARK_STALE_ALERT_SEC = 20
MARK_STALE_FLATTEN_SEC = 90
F_MARK_STALE_ALERT_SEC = 60
F_MARK_STALE_FLATTEN_SEC = 180
# The "tick before the close" used for the official exit price must be this recent (seconds); older (it predates an outage)
# and the trigger price is used instead. 0 = no limit.
PREV_TICK_MAX_AGE_SEC = 300
# A tick that raises something other than BrokerError no longer ends the process; its traceback is logged at most this often.
TICK_EXCEPTION_LOG_SEC = 60
# Schwab sim only: how often the refresh-token age is checked, and the day count at which it starts warning. Entries stop
# when the token would expire before FORCE_CLOSE_TIME + TOKEN_ENTRY_MARGIN_MIN (see schwab_health.py).
TOKEN_CHECK_SEC = 60
TOKEN_WARN_DAYS = 5.0
TOKEN_ENTRY_MARGIN_MIN = 30
# 2026-10-08: live ES (Schwab /ES candles, market_data.py). When a request fails the feed is treated as off for this many seconds
# (breakouts are then confirmed on SPX candles + the basis, never on the delayed Yahoo ES=F series), then tried again.
ES_LIVE_RETRY_SEC = 60
# Live /ES bars whose newest minute is older than this (during the regular session) mean the feed is stalled or delayed: treated as a
# failed request, so breakouts are confirmed on SPX candles instead of on a stale ES series.
ES_LIVE_MAX_BAR_AGE_SEC = 300
# One process per persona: a second launch against the same account would trade it with its own counters and caps. True = the
# bot takes a lock file in logs/ at start and a second copy refuses to run.
SINGLE_INSTANCE_LOCK = True
# 2026-10-12: at start-up the PC clock is compared with internet time (clock_check.py); the bot refuses to run when they differ by more than
# CLOCK_MAX_SKEW_SEC -- it schedules the open, the entry window, the hard stop and the Schwab login age from this clock, and a PC with the wrong
# date made a bot launched on a Monday see Saturday and exit silently. With no time source reachable it warns and carries on.
CLOCK_CHECK_ENABLED = True
CLOCK_MAX_SKEW_SEC = 120
# 2026-10-02: manual-override safety (closing/cancelling a position by hand in the broker UI, e.g.
# thinkorswim, while the bot is running). Every EXTERNAL_SYNC_SEC the bot compares its tracked
# spreads against what the broker actually holds; a position must look wrong on
# EXTERNAL_SYNC_CONFIRM consecutive syncs (and be older than EXTERNAL_MIN_AGE_SEC) before the bot
# acts, so a lagging positions API right after a fill can never make it drop a real position.
EXTERNAL_SYNC_ENABLED = True
EXTERNAL_SYNC_SEC = 10
EXTERNAL_SYNC_CONFIRM = 2
EXTERNAL_MIN_AGE_SEC = 30
# Before booking a hand-close (or a partial one) at the moment a stop/target/force-close fires, the bot
# reads the broker a second time this many seconds later and acts only if both reads agree.
EXTERNAL_PREFLIGHT_RECHECK_SEC = 2
# Adopt SPXW legs the bot has no record of (crash recovery) into its own books -- and so stop-manage and
# force-close them at the hard stop (FORCE_CLOSE_TIME). True on the dedicated Alpaca paper accounts; False on Schwab, whose account is
# shared with the 45DTE bot and whatever the owner trades by hand (allow-listed in tools/check_sync.py).
ADOPT_UNTRACKED_SPXW = False
# The same rule for single-stock option legs (Strategy F): with F on, every stock-option leg in the account that expires within F_MAX_DTE days used to be
# adopted and force-closed at the hard stop, whoever opened it. On Schwab that would be the owner's hand trades or a 45DTE spread near its expiration, so
# only legs today's saved state says F opened (or that F tracks now) are managed. True on Alpaca (allow-listed in tools/check_sync.py).
ADOPT_UNTRACKED_STOCK = False
# The staged exit's Bollinger-edge reversal check uses this %B level. It used to read D_BAND_TOUCH_PCT_B,
# the D/E ENTRY threshold, so tuning that for a test persona silently moved every strategy's exits.
EXIT_BB_TOUCH_PCT_B = 0.95
# After a position is recognized as closed by hand, that strategy takes no new entry for this long
# (a deliberate cool-down), then is ready to open again.
MANUAL_CLOSE_COOLDOWN_SEC = 120
# Create logs/MANUAL_HOLD (every persona) or logs/MANUAL_HOLD_<NAME> (one persona) and the bot sends
# NO closes -- stops, targets and floors stand down (and the force-close too, if
# MANUAL_HOLD_DELAYS_FORCE_CLOSE) -- until the file is deleted. Hand-closes are still detected and booked. Remember to delete it.
MANUAL_HOLD_FILE = "MANUAL_HOLD"
# False (the 2026-10-05 hard stop): a MANUAL_HOLD file does NOT delay the FORCE_CLOSE_TIME close -- the bot closes
# what it holds and stops at the hard stop regardless. True restores the older behaviour where the hold also
# stood the end-of-day close down and kept the bot up until the file was deleted or the session ended.
MANUAL_HOLD_DELAYS_FORCE_CLOSE = False
# 2026-10-01: user's own read on the paper-trading fill engine vs real-world execution -- the actual
# broker fill swings wildly from the trigger price during fast moves. The bot's OWN official
# pnl/realized_pnl (position_manager._close) now uses realistic_exit_price = the tick immediately
# before the one that triggered the exit (prev_price), not a flat slippage assumption -- the closest
# stand-in for where a live fill would realistically land. The real broker fill is still recorded
# separately as pnl_actual_fill, for comparison only. Identical to Alpaca/0DTE/config.py.
# 2026-11-06: which exit price the OFFICIAL P/L (pnl, realized_pnl, the daily loss limit, the loss streaks, the cards)
# is booked on. "prev_tick" = the tick before the trigger (above: the paper-trading stand-in); "fill" = the broker's
# actual fill (exit_price), what a LIVE account really made or lost. The other price always stays beside it in the row
# (exit_price / pnl_actual_fill, prev_tick_price / prev_tick_pnl).
# Schwab trades a real account, so its official P/L is the fill (Alpaca paper: "prev_tick").
EXIT_PNL_BASIS = "fill"
# 2026-11-06: resting exits. Right after an entry fills, the bot places ONE OCO at the broker to close the spread: a
# profit-target limit (buy to close at target_price) and a stop-limit (triggers at stop_price, limit a buffer above it),
# so the exit sits at the broker ready to execute even if this script is slow or down. The script's own rules keep
# running and cancel the OCO before closing early. Only a broker that can hold an OCO for a spread places one (Schwab
# live / dry-run); the simulator and Alpaca never do, whatever this says.
RESTING_EXITS = True                     # Schwab: the exit rests at the broker (Alpaca: False)
RESTING_EXIT_MODE = "oco"                # "oco": ask Schwab (previewOrder) whether it takes target + stop-limit on the spread, place it if so,
                                         #        else rest the profit target alone (the stop stays with the bot); "target": never try the OCO
RESTING_STOP_LIMIT_PCT_OF_WIDTH = 0.03   # the stop child's limit sits this fraction of the width above the stop ($0.15 on a $5 SPX spread)
RESTING_EXITS_FOR_STOCKS = False         # F stock spreads: their targets sit inside the quote noise and some names refuse penny prices
RESTING_EXIT_POLL_SEC = 5                # how often the resting order's status is read at the broker (12 requests/min per open spread)
RESTING_EXIT_CANCEL_WAIT_SEC = 5         # after cancelling the OCO ahead of a script close: wait this long for the broker to confirm
RESTING_EXIT_CANCEL_WAIT_FORCE_SEC = 20  # ... and this long at the hard stop, the stale-data watchdog and Ctrl-C (a single pass)
RESTING_EXIT_UNRESOLVED_GRACE_SEC = 120  # a spread gone from the account whose resting order cannot be read as filled or dead waits
                                         # this long before it is booked as a hand close at the last mark
RESTING_EXIT_MAX_ATTEMPTS = 2            # an OCO that dies without a fill is re-placed once; after that the exits are script-managed
LOG_DIR = "logs"

UNDERLYING = "SPX"
OPTION_ROOT = "SPXW"
SPX_SYMBOL = "^GSPC"
# 2026-10-01: Alpaca has no genuine SPX index bars on this plan (confirmed by broker.get_spot's own
# yfinance fallback for index underlyings) -- market_data.get_spx_candles_via_alpaca instead pulls
# REAL, high-frequency bars for SPY (the ETF, which Alpaca does serve directly on the free IEX feed)
# and scales them up to SPX-equivalent terms, same trick as the existing ES_TO_SPX basis shift. This
# replaces the high-frequency, every-tick, every-persona yf.download("^GSPC", ...) candle fetching
# that caused a real live incident (see Alpaca/0DTE/config.py for the full write-up). This folder
# already carries optional cross-broker ALPACA_API_KEY/SECRET (for the SPX option-chain gap below);
# the same credentials now also cover this. Identical to Alpaca/0DTE/config.py otherwise.
SPY_SYMBOL = "SPY"
SPX_SPY_RATIO_REFRESH_MIN = 60
SPX_SPY_RATIO_FALLBACK = 10.0
# 2026-10-01, same day: moving SPX candles onto Alpaca immediately pushed Alpaca's OWN data-API rate
# limit hard enough to also 429 this bot's unrelated TRADING-api calls (same account). Identical to
# Alpaca/0DTE/config.py -- see that file for the full write-up (market_data._alpaca_data_get retries
# 429/5xx with backoff; get_spx_candles_via_alpaca fetches one base 1-minute series and resamples
# locally instead of one Alpaca request per interval).
ALPACA_DATA_RETRIES = 3
ALPACA_DATA_RETRY_SEC = 0.5
ES_SYMBOL = "ES=F"
# 2026-09-25: Schwab's own futures quote symbol for the same instrument -- this folder already
# authenticates to Schwab, so live ES via market_data.get_es_candles_live works here directly (no
# cross-broker credentials needed, unlike the Alpaca folder's copy of this same mechanism).
ES_SCHWAB_SYMBOL = "/ES"

MARKET_OPEN = time(9, 30)
# 2026-10-01: real regular-trading-hours close, used to filter Alpaca's SPY bars down to the same
# session the SPX series they're standing in for actually trades -- NOT FORCE_CLOSE_TIME (this bot's
# own early close). Identical to Alpaca/0DTE/config.py.
MARKET_CLOSE = time(16, 0)
OVERNIGHT_SESSION_START = time(18, 0)
OPENING_RANGE_MINUTES = 15             # the opening range is the first N minutes after the open; Schwab live: Schwab data, 15-minute opening range, breaks from 09:45 ET (08:45 Central)
OVERNIGHT_ENTRY_END_MIN = 15           # the overnight-level setups end and the ORB phase begins N minutes after the open
ORB_LATE_SIGNAL_MAX_MIN = 10             # an ORB signal found on a replayed candle this recent is still taken (see scheduler.orb_signal)
# 2026-10-05: HARD STOP at 1:00 pm Eastern (12:00 noon Central), no matter what: every open spread is closed, every
# working order is cancelled and the bot exits for the day (it was 15:30). 2026-10-07: NEW ENTRIES stop at 12:00 noon
# Eastern (11:00 Central) -- the last hour (12:00-13:00 ET) only manages what is already open, then the hard stop.
# The window between the two is the NO_NEW_ENTRIES phase.
LAST_ENTRY_TIME = time(12, 0)     # 11:00 Central: last new entry (all strategies, F included)
FORCE_CLOSE_TIME = time(13, 0)    # 12:00 noon Central: everything closed, bot stops

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
# 2026-10-08: ONE index series, shared and complete. Every SPX candle, the opening range, the spot used by the data layer and
# the SPX-ES basis now come from Yahoo ^GSPC 1-minute bars (candles are resampled from them), downloaded ONCE for all personas
# into a shared snapshot (shared_cache.py) instead of once per persona, and a candle is emitted only when its LAST 1-minute bar
# has been seen in a snapshot fetched at least CANDLE_SETTLE_SEC after that bar ended (it used to be emitted at the clock
# boundary from whatever bars had arrived: the last minute moved a 5-minute close by >= 1 pt in 49% of candles).
#   SPX_CANDLE_SOURCE "yahoo" | "schwab" (2026-11-10: Schwab's own $SPX 1-minute price history through the folder's token, the
#   same read-only client the live ES feed uses; Yahoo the moment it cannot be read) | "spy" (the older Alpaca SPY bars x a
#   calibrated ratio, kept for rollback)
SPX_CANDLE_SOURCE = "schwab"                  # Schwab live: Schwab data, 15-minute opening range, breaks from 09:45 ET (08:45 Central)
SPX_SCHWAB_SYMBOL = "$SPX"
SPX_SCHWAB_LOOKBACK_MIN = 1440           # minutes of $SPX history asked of Schwab per request: covers the B/F/ORB windows; D/E's
                                         # multi-day window (D_E_LOOKBACK_MIN) is NOT covered, D/E stay on the Yahoo source
SPX_SCHWAB_MAX_BAR_AGE_SEC = 300         # a Schwab $SPX series whose newest bar is older than this (in session) counts as failed
SPX_SCHWAB_RETRY_SEC = 60                # after a failed $SPX request Yahoo is used and Schwab is not asked again for this long
CANDLE_SETTLE_SEC = 2.0
# While the bar that just closed is still missing from a session snapshot, re-download at most this often (shared by all personas).
CANDLE_RETRY_SEC = 3.0
# A candle that is still missing a 1-minute bar this long after it ended is accepted as it is (a data gap must not freeze
# the series, e.g. for D/E's multi-day EMA/Bollinger window); a younger incomplete candle is withheld.
CANDLE_GAP_TOLERANCE_SEC = 180
SHARED_DATA_ENABLED = True
SHARED_DATA_DIR = "shared_data"      # relative to this folder; every process that runs from it shares the snapshots
SHARED_LOCK_STALE_SEC = 30
SHARED_WAIT_SEC = 6

# 2026-10-01: a real live incident -- the SPX opening range broke hard downside at 10:00 ET and every
# persona stayed completely silent (no entries, no rejections, nothing in the journal). Root cause:
# yfinance's yf.download("^GSPC", ...) can come back empty with NO exception raised (Yahoo API
# throttling/outage; yfinance prints its own "possibly delisted" message internally, which is not the
# same as our code seeing a Python exception) -- _download's try/except never fires, the empty result
# gets cached and returned identically to a legitimate read, and orb_signal() silently iterates zero
# candles forever. Identical to Alpaca/0DTE/config.py -- see that file for the full rationale
# (market_data._download/_report_feed_health).
DATA_DOWNLOAD_RETRIES = 2
DATA_DOWNLOAD_RETRY_SEC = 1.0
DATA_FEED_REMINDER_SEC = 120

# ES_TO_SPX: ES overnight levels + (SPX - ES) basis, compared against live SPX candles -- or, once
# market_data.live_es_available() is true (this folder's own Schwab auth already covers it), ES's
# own candles directly for the real lead-time edge (scheduler.overnight_levels_for_signal).
# ES: compare ES candles directly against ES levels (Yahoo ES is ~10 min delayed) unconditionally.
BREAKOUT_LEVEL_SOURCE = "ES_TO_SPX"

# Every signal opens two independent spreads: A sells the ITM spread, B sells an OTM spread
# whose short strike sits one expected move (ATM straddle) away from spot.
# 2026-10-01: Strategy A removed -- 0% win rate across every Alpaca persona and every day of real
# trading data analyzed (41/41 losses, -$8,660), with no day ever net positive, meeting the user's own
# explicit bar. A's own code is deliberately left in place, unused, same as OVERNIGHT's retirement --
# reversible if ever revisited, not deleted. Identical to Alpaca/0DTE/config.py.
# 2026-10-15: Schwab 0DTE trades B (SPX) and F (single-stock ORB) only -- C, D and E stay off. With MAX_OPEN_POSITIONS_TOTAL = 1 below, one
# trade is open at a time across both. F had never run on Schwab before this date (allow-listed in tools/check_sync.py).
STRATEGIES = ("B", "C", "F")
# Strategies that trade single-stock option roots with one position per ticker. They are excluded from
# every SPX signal fan-out (strategy.spx_strategies) and from SPXW adoption; cards append the ticker.
STOCK_STRATEGIES = ("F",)
ALL_STRATEGIES = ("A", "B", "C", "D", "E", "F")
STRATEGY_LABELS = {
    "A": "A - ITM", "B": "B - OTM (expected move)",
    "C": "C - ES overnight-break (OTM, same as B)",
    "D": "D - MA/Bollinger reversion, trend-gated",
    "E": "E - MA/Bollinger reversion, 5-min chop confirm",
    "F": "F - STOCK ORB",
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
# 2026-10-01: narrowing every strategy to a uniform 5 trades/day/persona across the board -- enough
# live data gathered to start cutting toward an actual live-trading version rather than wide-open
# exploratory limits. Identical to Alpaca/0DTE/config.py.
MAX_TRADES_PER_DAY = 3
# 2026-10-07: optional ONE cap across every strategy of a persona (B, C, D, E and F together): once this many trades have
# been taken today (closed, plus those still open) no strategy enters again. OFF (None): the same day the user asked for
# per-strategy limits instead -- B 5 a day (MAX_TRADES_PER_DAY above), C, D, E and F 3 a day each (LIMITS_BY_STRATEGY below).
MAX_TRADES_TOTAL_PER_DAY = 3
# 2026-10-08: at most this many trades OPEN at one time across ALL of a persona's strategies (B, C, D, E and F together;
# before this each of B-E had its own single slot and F up to 3, so 7 could be open). None = off. A trade being
# decided is refused while this many are open; positions adopted from the broker at a restart are not counted against a
# new entry's decision beyond being open.
MAX_OPEN_POSITIONS_TOTAL = 1           # 2026-10-15: Schwab 0DTE (live go-live): ONE trade open at a time across B and F (Alpaca: 2)
MAX_CONSECUTIVE_LOSSES = 2
# Per-strategy overrides of the two counters plus a cool-down (minutes after that strategy's last exit
# before it may enter again). 2026-09-21: 11-16 A trades per persona re-bought the same breakout every
# five minutes; capping at 3 trades would have cut the day's loss by 84%.
LIMITS_BY_STRATEGY = {
    "A": {"MAX_TRADES_PER_DAY": 3, "MAX_CONSECUTIVE_LOSSES": 2, "COOLDOWN_MIN": 10},   # 2026-09-22: 5 trades to test the exit floors
    "B": {},
    # 2026-09-25: 5 trades/day per strategy for the new multi-strategy personas (each active
    # strategy independently gets its own 5/day allowance, not a combined cap).
    # 2026-10-07: B keeps 5 a day (the global MAX_TRADES_PER_DAY); every other strategy gets 3 a day.
    "C": {"MAX_TRADES_PER_DAY": 3},
    "D": {"MAX_TRADES_PER_DAY": 3},
    "E": {"MAX_TRADES_PER_DAY": 3},
    # 2026-09-28 Strategy F: entries counted at ENTRY, 7 consecutive losses across all F tickers,
    # up to 3 F positions open at once in different tickers. No COOLDOWN_MIN (strategy-wide).
    "F": {"MAX_TRADES_PER_DAY": 3, "MAX_CONSECUTIVE_LOSSES": 7, "MAX_CONCURRENT_POSITIONS": 3},
}
# 2026-10-15 (Schwab live go-live): acceptable daily loss is 1% and the daily goal is 2% of the day's start equity -- the capital cap (see
# LIVE_CAPITAL_*), so $10 and $20 at the $1,000 minimum. Alpaca keeps 10% and no target (allow-listed in tools/check_sync.py).
DAILY_LOSS_LIMIT_PCT = 0.025
# Per-strategy concurrency cap and the limit() fallback for strategies without their own entry (A-E are
# single-slot by dict-key construction; only F's entry path consults this).
MAX_CONCURRENT_POSITIONS = 1
# Strategies whose P/L -- realized, and unrealized from the marks the tick already fetched -- is removed
# from the combined 10% daily-loss basis. HALTS False: an A+B limit hit does not stop them either.
# 2026-10-15: on Schwab nothing is exempt -- F's P/L counts toward the loss limit and F is stopped by it like B (Alpaca: F exempt).
DAILY_LOSS_LIMIT_EXEMPT = ()
DAILY_LOSS_LIMIT_HALTS_EXEMPT = False
# Stop NEW entries (B and F) once the day's realized P/L reaches this fraction of the day's start equity. Open trades keep their normal exits.
DAILY_PROFIT_TARGET_PCT = 0.05
# The loss limit is measured on THIS bot's P/L (realized + its open trades' marks), not on the account's value: the Schwab account also holds the owner's
# hand trades and the 45DTE bot, and at 1% of the capital cap ($10 at the $1,000 minimum) their moves would stop B and F for the day.
DAILY_LOSS_BASIS = "bot"

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
# than ON_BREAK's wick-aware break/pullback/momentum machine. D and E are NOT setup-driven: their own
# signal comes from check_mean_reversion_entry each tick (setup "MA_BB"/"MA_BB_CHOP"), not from a
# fired breakout. They still MUST be listed here, same as F: enter_leg's `.get(strat, (setup,))`
# default treats an ABSENT strategy as eligible for every SPX setup it's ever called with -- since
# dispatch_entry/enter() route every fired ORB/ON_BREAK signal through strategy.spx_strategies()
# (which includes D and E), an absent entry silently let D and E sell a same-day B-clone spread
# tagged with their own strategy letter on ANY breakout signal, not just their own mean-reversion
# one. 2026-09-30: found live (a real D EXIT event tagged setup="ORB", structurally impossible) --
# fixed by listing each one's OWN setup only. Identical to Alpaca/0DTE/config.py.
# F MUST be listed: enter_leg's `.get(strat, (setup,))` default treats an ABSENT strategy as eligible for
# every SPX setup, which would sell an SPX B-clone labelled F on every ORB/ON_BREAK signal.
SETUPS_BY_STRATEGY = {"A": ("ORB", "ON_BREAK"), "B": ("ON_BREAK", "ORB"), "C": ("ES_BREAK",),
                      "D": ("MA_BB",), "E": ("MA_BB_CHOP",), "F": ("STOCK_ORB",)}
# Both entry kinds for A: the 2026-09-21 replay showed a trend day with one momentum break and no pullback.
ORB_ENTRY_KINDS_BY_STRATEGY = {"A": ("MOMENTUM", "PULLBACK"), "B": ("MOMENTUM", "PULLBACK"),
                               "F": ("MOMENTUM", "PULLBACK")}

PROFIT_TARGET = 0.30
STOP_LOSS = 0.30
# Real seconds the spread price must sit at/above the stop before it actually closes (default 0 =
# immediate, today's behavior everywhere except A). 2026-09-23: five personas hit an identical
# ON_BREAK entry; two were stopped out within the same minute by what looks like a single noisy
# print, while the other three rode the same move to a runner win. A requires ~30s to confirm a
# stop. Named in SECONDS (not a raw tick count) specifically so this can't silently drift if
# TICK_SECONDS ever changes -- position_manager derives the actual tick count from the current rate.
STOP_CONFIRM_SECONDS = 0
# 2026-10-07 (staged exit only; 0 = off, today's behaviour, for every strategy except F -- see F_BASE_TUNING):
#   STOP_MIN_HOLD_SEC          for this long after entry the stop only fires at STOP_HOLD_EMERGENCY_MULT x its normal
#                              distance (a real collapse still exits at once; quote noise right after the fill does not)
#   STOP_CONFIRM_TIME_SEC      real seconds the price must stay at/above the stop before it closes. Wall-clock, unlike
#                              STOP_CONFIRM_SECONDS (which is divided by TICK_SECONDS and so assumes a price every
#                              tick -- F is priced only every F_PRICE_EVERY_SEC)
STOP_MIN_HOLD_SEC = 0
STOP_HOLD_EMERGENCY_MULT = 3.0
STOP_CONFIRM_TIME_SEC = 0
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
# Registry placeholders so strategy.exit_levels("F") can never raise; F's LIVE target/stop are width-scaled
# per position (strategy.f_exit_params), never these.
F_PROFIT_TARGET = B_PROFIT_TARGET
F_STOP_LOSS = B_STOP_LOSS
RUNNER_ENABLED = True
RUNNER_MIN_CONTRACTS = 2
# Fraction of the position booked at the target when momentum continues; the rest runs with the stop at
# the target level and the trail. A runs the whole position (the trail was the only profit source on
# 2026-09-21: +$2,650 from six exits vs +$180 from 34 fixed-target exits); B books half as before.
RUNNER_CLOSE_FRACTION_BY_STRATEGY = {"A": 0.0, "B": 0.5, "F": 0.5}   # F: runner rules like B
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

# ------------------------------------------------------------ staged exit system (every strategy)
# 2026-09-29/30: replaces the flat target/stop/lock/runner sequence above for EVERY strategy -- A
# included, per the user's own instruction ("A should be tuned the same, I use the same signals for
# A"). Modeled directly on a real manual trade (10:01 SPX call credit spread, entered ~2.10, rode a
# downtrend, stopped at 1.50 after entering the stop order at 1.40 once the 30-cent target was hit):
# ride ordinary noise on just the raw stop, tighten once profit is real AND momentum shows signs of
# turning, lock a hard floor once the named target is hit ("no matter what" -- the user's own words),
# then let a genuine continuation run behind a trail that only ratchets to a new best price once that
# price has actually HELD (not a brief wiggle) for a while with momentum still continuing.
#
# Stage 0 (always): raw stop-loss, unconditional -- $0.40 from entry (on a $5-wide reference spread).
# Stage 1 (profit >= $0.20 -- armed once, stays armed): exits on EITHER a $0.15 giveback from the best
#   price seen, OR the moment candles/RSI/Bollinger-edge show the move reversing -- whichever first.
# Stage 2 (profit >= $0.30 target -- armed once, stays armed, supersedes stage 1): a hard,
#   unconditional floor at $0.25 profit, no momentum check.
# Stage 3 (stage 2 armed, still running past the floor): trails at $0.15 off a CONFIRMED best price --
#   that anchor only advances to a new, deeper best price after it has held (no further improvement)
#   for STAGE3_CONFIRM_SECONDS with momentum still continuing, so a brief spike that reverses right
#   back doesn't tighten the trail and kick a real runner out on ordinary wiggle.
#
# Identical to Alpaca/0DTE/config.py (tools/check_sync.py compares every value); see that file for the
# full rationale. All four dollar levels are fractions of spread width (same convention as F_xxx_PCT
# below), so the same constants apply unscaled to every strategy's own width(s).
STAGED_EXIT_ENABLED = True
STAGE0_STOP_PCT = 0.08          # $0.40 on $5
STAGE1_ARM_PCT = 0.04           # $0.20 on $5
STAGE1_TRAIL_PCT = 0.03         # $0.15 on $5
STAGE2_TARGET_PCT = 0.06        # $0.30 on $5 -- matches PROFIT_TARGET/B_PROFIT_TARGET already
STAGE2_FLOOR_PCT = 0.05         # $0.25 on $5
STAGE3_TRAIL_PCT = 0.03         # $0.15 on $5
STAGE3_CONFIRM_SECONDS = 30
STAGE_EXIT_FLOOR = 0.05         # floor under every width-scaled level above, same role as F_EXIT_FLOOR

# Stage 1's momentum-reversal read: candles/RSI (strategy.momentum_reversing) plus a Bollinger-edge
# proximity check (strategy.bb_edge_reversal_risk) using its OWN, shorter period -- distinct from
# D/E's own 30-period ENTRY-signal read (D_BB_PERIOD). Same 2 std dev and touch threshold as D/E
# (D_BB_STD, D_BAND_TOUCH_PCT_B), same 5-minute candle interval (D_E_CANDLE_INTERVAL).
EXIT_BB_PERIOD = 20
EXIT_BB_STD = 2.0
RSI_PERIOD = 14
RSI_REVERSAL_PTS = 5.0

# ------------------------------------------------------------------- quote-jump sanity gate
# 2026-09-30: ASTRA reported a spread price of 5.21 one tick, then exited ~6.10 the very next tick --
# too large a move for that one second's real underlying action (confirmed against the live chart).
# spread_prices() (scheduler.py) fetches a fresh option-chain quote every tick with zero validation --
# a single stale/bad print from the indicative feed reaches every exit check (stop, target, floor,
# trail, staged or legacy) immediately, and self-corrects on the next tick with no trace except the
# real (bad) exit it triggered. This does not distinguish "bad print" from "genuinely fast real move"
# by inspecting the quote itself (no bid/ask width check yet -- see the TODO below); it uses the same
# tool already trusted elsewhere in this file for the identical class of problem (STOP_CONFIRM_SECONDS,
# STAGE3_CONFIRM_SECONDS): require a second, means a real move; a one-tick spike that doesn't repeat is
# suspect and gets held rather than acted on. Identical to Alpaca/0DTE/config.py -- see that file for
# the full rationale (position_manager._sanitize_quote), including the post-review redesign notes:
# an earlier version scaled the allowed jump by real elapsed wall-clock time (including time spent
# stuck pending), which both silently defeated confirmation over time AND left Strategy F effectively
# ungated at its own normal ~15s cadence. Fixed with a FIXED, capped per-strategy cadence multiplier
# (QUOTE_JUMP_MAX_NOMINAL_TICKS) plus an explicit QUOTE_JUMP_MAX_HOLD_SECONDS backstop measured from
# when a position first got stuck (not from its last accepted tick), so it still can't hang forever.
# TODO: also sanity-check the raw bid/ask width itself (something like F's quote_blown_out), not just
# mid-to-mid movement -- a blown-out quote can be wrong without necessarily being a big jump from the
# last accepted price (e.g. two consecutive bad prints).
QUOTE_JUMP_SANITY_PCT = 0.06     # $0.30 on $5 per nominal tick -- same magnitude as STAGE2_TARGET_PCT
QUOTE_JUMP_SANITY_FLOOR = 0.05   # floor so a thin/cheap spread (e.g. F on a $1 width) isn't gated on noise
QUOTE_JUMP_MAX_NOMINAL_TICKS = 5.0   # caps F's real ~15x cadence gap so its bound stays meaningful
QUOTE_JUMP_CONFIRM_SECONDS = 2
QUOTE_JUMP_MAX_HOLD_SECONDS = 6      # backstop: never withhold a tick this long, no matter what

# Per-strategy overrides of the exit knobs above (strategy.exit_setting). A, from the 2026-09-21 replay:
# lock arms at +0.30 and tolerates a 0.35 giveback (about one quote width), no candle-against exit, the
# runner always starts at the target, and no momentum-slowdown exit. That set turned the day's real entries
# from -$16.9k to +$13.5k on the model; the shared 0.15/0.10 lock cut every winner at about +0.10. B keeps
# the shared defaults until it has its own data.
# 2026-09-29: RETUNED tighter (arm +0.15, giveback 0.05), reversing the above -- A ran a full week at
# 0% win rate, with the giveback repeatedly wiping out gains that had gotten close to target (one
# traced case: locked profit around +0.32, gave it ALL back through entry to a real loss before the
# looser 0.35 giveback ever triggered). The 2026-09-21 tuning solved "the lock cuts winners too early";
# this solves "the lock doesn't cut losers running back through breakeven soon enough" -- a genuine
# trade-off, not a strict improvement, and worth re-examining once there's a full week of data on it.
# Profit floor: once the trade is `arm` in profit, a hard exit floor sits at `floor` profit while the position
# (and any runner) keeps going. Keyed by spread width: {width: (arm, floor)}. Empty = off.
PROFIT_FLOOR_BY_WIDTH: dict = {}
# Stale timer: STALE_TIMER_MIN minutes after entry, if the target has not been hit and the trade shows any
# profit, a floor at STALE_FLOOR profit is set (2026-09-22: trades that made +0.20 gave it all back to a loss).
STALE_TIMER_MIN = None
STALE_FLOOR = 0.05

A_BASE_TUNING = {"PROFIT_LOCK_ARM": 0.15, "PROFIT_LOCK_GIVEBACK": 0.05, "PROFIT_LOCK_ON_MOMENTUM_FLIP": False,
                 "RUNNER_MOMENTUM_GATE": False, "RUNNER_SLOWDOWN_EXIT": False, "RUNNER_TIGHTEN_ENABLED": True,
                 "STOP_CONFIRM_SECONDS": 30, "MOMENTUM_CONFIRM_ENABLED": True}
B_BASE_TUNING = {"REENTRY_ON_CONTINUATION_ENABLED": True}
C_BASE_TUNING: dict = {}
D_BASE_TUNING: dict = {}
E_BASE_TUNING: dict = {}
# F: the shared exit knobs at the module defaults, evaluated on the TICKER's own 5-minute candles. Lock
# arm/giveback/trail are per-position width-scaled overrides (strategy.f_exit_params), not tuned here.
# 2026-10-07: all three F stop-outs of 10/6-10/7 happened 5-52 s after entry, with MFE ~0, while the underlying never
# threatened the short strike: the quoted bid/ask on the vertical (0.05-0.29) was as wide as the stop (0.08-0.25), so a
# single wide print stopped the trade. F's stop now waits out the first minute (emergency stop at 3x) and must hold
# for 30 s; and an entry is refused unless the quoted spread is at most F_MAX_QUOTE_SPREAD_STOP_RATIO of the stop.
F_BASE_TUNING: dict = {"STOP_MIN_HOLD_SEC": 60, "STOP_HOLD_EMERGENCY_MULT": 3.0, "STOP_CONFIRM_TIME_SEC": 30}
EXIT_TUNING_BY_STRATEGY = {
    "A": dict(A_BASE_TUNING), "B": dict(B_BASE_TUNING),
    "C": dict(C_BASE_TUNING), "D": dict(D_BASE_TUNING), "E": dict(E_BASE_TUNING),
    "F": dict(F_BASE_TUNING),
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
# 2026-09-25: this is a ROLLING window, same as any 30-period indicator on a real chart -- it does
# NOT reset at the open and rebuild from zero each session (that was a bug, not a design choice: D/E
# should be able to fire any time during the day, using the prior session's tail candles to fill the
# window early in a new day, not sitting out until today alone has accumulated 30 bars). 4000 minutes
# (~2.75 days) comfortably reaches across a normal overnight gap AND a long weekend (Friday close ->
# Monday open). D_E_MIN_CANDLES stays as a safety net for a genuine data outage -- it should now
# essentially always already be satisfied.
D_E_LOOKBACK_MIN = 6000
D_E_MIN_CANDLES = 30
E_CONFIRM_CANDLES = 2
# 2026-10-02 (C/D/E never fired in the first two trending sessions). Three test levers, all defaulting to
# the original behavior:
#   D_E_LOOKBACK_MIN was 4000, which does NOT reach back across a weekend (Fri 15:20 at Monday's open): D/E
#     had fewer than D_E_MIN_CANDLES bars until ~11:25 every Monday. 6000 (~4.2 days) covers it.
#   E_USE_TREND_GATE False -> E decides direction from the side of the MA plus the band touch alone (no
#     RISING/FALLING/CHOP gate). With the gate on, E needs D's exact setup AND two same-colour candles
#     moving away from the band, which pull %B back inside the band -- it essentially never fires.
#   C_ENTRY_END_MIN: how many minutes after the open Strategy C may still take an ES-overnight-level break
#     (30 = only the 09:30-10:00 window, the original behavior; 60 = until 10:30).
E_USE_TREND_GATE = True
C_ENTRY_END_MIN = 30

# ------------------------------------------------------ Strategy F: stock opening-range breakout
# Identical to Alpaca/0DTE/config.py (tools/check_sync.py compares every value). F runs on ASTRA only;
# see that file for the full description of each knob.
F_UNIVERSE_PATH = "data/orb_universe.json"   # relative to this folder; missing/invalid -> F off for the day
F_PREWARM_TIME = time(9, 20)      # pre-open: warm the scanner's 1-min history pages + daily bars (background thread)
F_RANK_TIME = time(9, 45)         # end of the 15-min OR: score + rank the universe once (background thread)
F_RANK_GRACE_SEC = 45             # wait for Yahoo's lagging 09:44 bar before scoring
F_SCAN_RETRY_SEC = 60             # retry cadence after a failed scan, and while the 1-min bars still lag the OR15
F_SCAN_MAX_RETRIES = 5            # failed (raising) scans before F is off for the day; lagging scans never count
F_SCAN_WITH_CHAINS = True         # score the 10-pt option-quality component through broker chains (in the scan thread)
F_CHAIN_CALL_GAP_SEC = 0.25       # pacing between per-ticker chain calls inside the scan thread (429 protection)
F_MIN_SCORE = 60.0                # qualification threshold on the raw 0-100 score (order = score_with_tier)
F_WATCH_TOP = 5                   # watch the top N qualifiers; entries in this rank order
F_OR_MINUTES = 15                 # opening range seeding each ticker's OrbSetup (scanner or15)
F_CANDLE_MINUTES = 5              # breakout/confirmation candle, resampled from the scanner's 1-min bars
F_CANDLE_MAX_WAIT_SEC = 90        # a candle still missing its last 1-min bar this long after its end is logged and no longer polled every DATA_CACHE_SEC (re-checked each candle); never accepted early
F_BARS_SESSIONS = 6               # today + prior sessions kept per watched ticker (normal-volume baseline, EMA warm-up)
F_SIGNAL_TTL_MIN = 30             # a confirmed break not entered within this window is dropped
F_REQUIRE_SCANNER_DIRECTION = False   # True: only trade a break in the scanner's likely direction (direction_match is always journaled)
F_REENTRY_MOMENTUM_MINUTES = 4    # after an F exit: this many consecutive completed 1-min closes beyond the previous close, still beyond the OR level, re-signal the ticker
F_MIN_DTE = 0                     # broker.get_expirations(ticker, F_MIN_DTE, F_MAX_DTE)[0] is traded
F_MAX_DTE = 7
F_EM_WINDOW_PCT = 0.03            # +/- fraction of spot for the ATM straddle chains and the strike-increment read
F_WIDTH_PCT = 0.01                # spread width ~1% of spot ...
F_MIN_WIDTH = 1.0                 # ... snapped to the chain's strike increment, never below $1
F_CREDIT_PCT_RANGE = (0.13, 0.22) # entry credit band as a fraction of width; lower bound = work_entry floor
F_MAX_STRIKE_STEPS = 20           # strike-walk iterations before F_NO_STRIKE_IN_RANGE
# 2026-10-16: how far from spot the short strike may sit, as a multiple of the expected move (it used to be a fixed 2x). Normal names 1.8x.
# A COARSE grid -- the strike step is so large that the EM is under F_COARSE_GRID_EM_STEPS steps (e.g. a $2.50 grid on a $2.85 EM) -- cannot land
# a strike near 1 EM, so it gets the wider cap instead of being rejected every time (10/16: a $171 name was refused at 2.18x EM).
F_EM_CAP_MULT = 1.8
F_EM_CAP_MULT_COARSE = 2.5
F_COARSE_GRID_EM_STEPS = 2.0
F_TARGET_PCT = 0.06               # profit target as a fraction of width (0.30 on $5)
F_STOP_PCT = 0.10                 # stop as a fraction of width (0.50 on $5)
F_LOCK_ARM_PCT = 0.03             # profit-lock arm as a fraction of width (0.15 on $5)
F_LOCK_GIVEBACK_PCT = 0.02        # profit-lock giveback as a fraction of width (0.10 on $5)
F_TRAIL_PCT_OF_WIDTH = 0.06       # runner trail as a fraction of width (0.30 on $5)
F_EXIT_FLOOR = 0.05               # floor under every width-scaled exit level above
F_BREAK_VOLUME_RATIO = 1.0        # break-candle volume >= ratio x same-window mean over prior sessions
F_MIN_REMAINING_EM_FRAC = 0.35    # remaining expected move must still be >= this fraction of the day's EM
F_MAX_QUOTE_SPREAD_FRAC = 0.05    # reject when (ask_side - bid_side) of the spread quote > this fraction of width (was 0.12)
F_MAX_QUOTE_SPREAD_STOP_RATIO = 0.5   # ... or > this fraction of F's stop distance, so the stop sits >= 2x above the quote noise
F_ENTRY_STEP_PCT = 0.01           # work_entry limit step as a fraction of width, min $0.01 (0.05 on $5)
F_ENTRY_STEP_SEC = 15             # work_entry fill wait per step for F orders (SPX keeps ENTRY_STEP_SEC) ...
F_ENTRY_TIMEOUT_SEC = 45          # ... so this timeout is honoured after three steps instead of overrunning
F_HALF_SIZE = False               # contracts_for half_size flag for F (TESTING cap 3 instead of 6)
F_PRICE_EVERY_SEC = 15            # min seconds between chain marks per open F ticker (bypassed on force close)
F_MAX_ENTRIES_PER_TICKER = None   # None = re-entry allowed via a fresh confirmed break or F_REENTRY_MOMENTUM_MINUTES
F_MAX_ERRORS_PER_DAY = 20         # consecutive F_ERRORs before F disables itself for the day

# ---------------------------------------------------------------- testing-phase contract sizing
# 2026-09-25: while C/D/E are being live-tested, contract size is capped well below the normal
# tier-based sizing -- half that cap again for Strategy D (always) and Strategy E (choppy-day
# variant). Supersedes MAX_CONTRACTS_PER_TRADE while TESTING_MODE is True; revert once testing ends.
TESTING_MODE = True
TESTING_MAX_CONTRACTS = 6
TESTING_HALF_SIZE_MAX_CONTRACTS = 3
