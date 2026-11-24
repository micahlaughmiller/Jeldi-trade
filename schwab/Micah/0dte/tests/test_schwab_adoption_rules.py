"""Schwab-only: this folder's real (un-pinned) ADOPT_UNTRACKED_SPXW. The shared tests pin it to the Alpaca
default; this guards the value each broker folder actually ships with."""

import config


def test_schwab_adoption_default():
    assert config.ADOPT_UNTRACKED_SPXW is False
    assert config.ADOPT_UNTRACKED_STOCK is False                 # 2026-10-15: F never adopts a stock leg it did not open on the shared account


def test_the_shipped_strategies_and_open_cap():
    """2026-10-15: Schwab 0DTE trades B and F, one trade open at a time (live go-live); 2026-11-12: C as well. Run with SCHWAB_MODE=sim,
    the shipped default."""
    assert config.STRATEGIES == ("B", "C", "F") and "F" in config.STOCK_STRATEGIES
    assert config.MAX_OPEN_POSITIONS_TOTAL == 1


def test_the_shipped_daily_limits():
    """2026-10-15: F follows the loss limit like B (nothing exempt); 2026-11-12: acceptable daily loss 2.5%, daily goal 5%."""
    assert config.DAILY_LOSS_LIMIT_PCT == 0.025
    assert config.DAILY_LOSS_LIMIT_EXEMPT == ()
    assert config.DAILY_PROFIT_TARGET_PCT == 0.05
    assert config.DAILY_LOSS_BASIS == "bot"                       # not the account's value: it also holds hand trades and the 45DTE bot


def test_the_shipped_trade_limits():
    """2026-11-12 (edited on GitHub): 3 trades a day per strategy and across all of them, 2 consecutive losses, A's own 3 / 2 / 10-minute
    limits, STOP_LOSS 0.30 -- Strategy A's stop: B, C and F still read B_STOP_LOSS."""
    assert config.MAX_TRADES_PER_DAY == 3 and config.MAX_TRADES_TOTAL_PER_DAY == 3 and config.MAX_CONSECUTIVE_LOSSES == 2
    assert config.LIMITS_BY_STRATEGY["A"] == {"MAX_TRADES_PER_DAY": 3, "MAX_CONSECUTIVE_LOSSES": 2, "COOLDOWN_MIN": 10}
    assert config.STOP_LOSS == 0.30 and config.B_STOP_LOSS == 0.50 and config.C_STOP_LOSS == config.B_STOP_LOSS
    assert config.MAX_TRADES_PER_DAY_SHARED == {("B", "C"): 3}                       # B and C: at most 3 trades a day between them


def test_the_shipped_close_pricing():
    """2026-11-16 (Micah: "buy the mid or plus 5 cents of mid"): a profit exit works two orders at the mid + 0.05 on the grid, then goes
    marketable; a stop / hard stop / flatten is marketable at once. The exit rules themselves are unchanged (arm at $0.20)."""
    assert config.CLOSE_PRICE_BASIS == "mid" and config.CLOSE_MID_OFFSET == 0.05 and config.CLOSE_MID_ATTEMPTS == 2
    assert config.CLOSE_SLIPPAGE == 0.03 and config.CLOSE_URGENT_REASON_KEYWORDS == ("STOP", "FORCE", "FLATTEN", "INTERRUPT")


def test_the_shipped_runner_package():
    """2026-11-18 (Micah: "build it and test it as recommended"): resting target credit - 0.60, +0.10 floor and 0.25 trails after +0.20,
    no stage-2 floor, the 2-minute structure break as the only reversal read."""
    assert config.B_PROFIT_TARGET == 0.60 and config.C_PROFIT_TARGET == 0.60 and config.B_STOP_LOSS == 0.50
    assert config.STAGE1_ARM_PCT == 0.04 and config.STAGE1_FLOOR_PCT == 0.02 and config.STAGE1_TRAIL_PCT == 0.05
    assert config.STAGE2_FLOOR_ENABLED is False and config.STAGE3_TRAIL_PCT == 0.05
    assert config.EXIT_REVERSAL_MODE == "structure" and config.EXIT_STRUCTURE_INTERVAL == "2m" and config.EXIT_STRUCTURE_LOOKBACK == 3


def test_the_shipped_exit_booking_and_resting_exits():
    """2026-11-06: a real account's result is the fill, and the exit rests at Schwab as an OCO right after the entry fills."""
    assert config.EXIT_PNL_BASIS == "fill"
    assert config.RESTING_EXITS is True
    assert config.RESTING_STOP_LIMIT_PCT_OF_WIDTH == 0.03 and config.RESTING_EXIT_MAX_ATTEMPTS == 2


def test_the_shipped_data_source_and_opening_range():
    """2026-11-10: live Schwab data for everything, and a 15-minute opening range with breaks tradable from 09:45 ET (08:45 Central)."""
    assert config.SPX_CANDLE_SOURCE == "schwab" and config.SPX_SCHWAB_SYMBOL == "$SPX"
    assert config.OPENING_RANGE_MINUTES == 15 and config.OVERNIGHT_ENTRY_END_MIN == 15


def test_schwab_runs_the_orb_machine_on_2_minute_candles_and_confirms_a_break_by_0950():
    """2026-11-24: on 5-minute candles the earliest ORB entry was 09:55 (break 09:50, confirm 09:55). On 2-minute candles the
    09:46-09:48 candle breaks the 15-minute range and the 09:48-09:50 candle confirms it: the signal fires at 09:50."""
    from datetime import datetime, timedelta
    import pandas as pd
    import market_data
    import scheduler
    import strategy
    assert config.ORB_CANDLE_INTERVAL == "2m" and config.ORB_SETUP_TIMEOUT_CANDLES == 15
    assert config.OPENING_RANGE_MINUTES == 15
    ET = config.ET

    def t(h, m):
        return datetime(2026, 11, 24, h, m, tzinfo=ET)

    bot = scheduler.Bot.__new__(scheduler.Bot)
    bot.orb = strategy.Levels(6700.0, 6690.0, t(9, 45))
    bot.orb_setup = strategy.OrbSetup(6700.0, 6690.0)
    bot.last_orb_candle = None
    rows = [(t(9, 30 + 2 * i), 6695.0, 6699.0, 6691.0, 6695.0) for i in range(8)]          # 09:30..09:44 inside the range
    rows += [(t(9, 46), 6698.0, 6704.0, 6697.0, 6703.0)]                                     # 09:46-09:48 closes above 6700: armed
    frame = lambda rows: pd.DataFrame([{"open": o, "high": h, "low": l, "close": c, "volume": 1.0} for _, o, h, l, c in rows],
                                      index=pd.DatetimeIndex([r[0] for r in rows]))
    data = {"f": frame(rows)}
    import pytest
    mp = pytest.MonkeyPatch()
    mp.setattr(market_data, "get_candles", lambda *a, **k: data["f"])
    try:
        assert bot.orb_signal(t(9, 48) + timedelta(seconds=5)) is None and bot.orb_setup.state == strategy.OrbSetup.BROKEN
        data["f"] = frame(rows + [(t(9, 48), 6703.0, 6709.0, 6702.0, 6708.0)])               # 09:48-09:50 closes beyond the break close
        signal = bot.orb_signal(t(9, 50) + timedelta(seconds=5))
    finally:
        mp.undo()
    assert signal == (strategy.BULLISH, t(9, 48), strategy.MOMENTUM)
