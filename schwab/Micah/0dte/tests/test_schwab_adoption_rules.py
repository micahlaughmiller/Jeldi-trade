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


def test_the_shipped_exit_booking_and_resting_exits():
    """2026-11-06: a real account's result is the fill, and the exit rests at Schwab as an OCO right after the entry fills."""
    assert config.EXIT_PNL_BASIS == "fill"
    assert config.RESTING_EXITS is True
    assert config.RESTING_STOP_LIMIT_PCT_OF_WIDTH == 0.03 and config.RESTING_EXIT_MAX_ATTEMPTS == 2


def test_the_shipped_data_source_and_opening_range():
    """2026-11-10: live Schwab data for everything, and a 15-minute opening range with breaks tradable from 09:45 ET (08:45 Central)."""
    assert config.SPX_CANDLE_SOURCE == "schwab" and config.SPX_SCHWAB_SYMBOL == "$SPX"
    assert config.OPENING_RANGE_MINUTES == 15 and config.OVERNIGHT_ENTRY_END_MIN == 15
