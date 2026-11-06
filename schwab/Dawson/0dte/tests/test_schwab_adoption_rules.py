"""Schwab-only: this folder's real (un-pinned) ADOPT_UNTRACKED_SPXW. The shared tests pin it to the Alpaca
default; this guards the value each broker folder actually ships with."""

import config


def test_schwab_adoption_default():
    assert config.ADOPT_UNTRACKED_SPXW is False
    assert config.ADOPT_UNTRACKED_STOCK is False                 # 2026-10-15: F never adopts a stock leg it did not open on the shared account


def test_the_shipped_strategies_and_open_cap():
    """2026-10-15: Schwab 0DTE trades B and F only, one trade open at a time (live go-live). Run with SCHWAB_MODE=sim, the shipped default."""
    assert config.STRATEGIES == ("B", "F") and "F" in config.STOCK_STRATEGIES
    assert config.MAX_OPEN_POSITIONS_TOTAL == 1


def test_the_shipped_daily_limits():
    """2026-10-15: acceptable daily loss 1%, daily goal 2%, and F follows the loss limit like B (nothing exempt)."""
    assert config.DAILY_LOSS_LIMIT_PCT == 0.01
    assert config.DAILY_LOSS_LIMIT_EXEMPT == ()
    assert config.DAILY_PROFIT_TARGET_PCT == 0.02
    assert config.DAILY_LOSS_BASIS == "bot"                       # not the account's value: it also holds hand trades and the 45DTE bot


def test_the_shipped_exit_booking_and_resting_exits():
    """2026-11-06: a real account's result is the fill, and the exit rests at Schwab as an OCO right after the entry fills."""
    assert config.EXIT_PNL_BASIS == "fill"
    assert config.RESTING_EXITS is True
    assert config.RESTING_STOP_LIMIT_PCT_OF_WIDTH == 0.03 and config.RESTING_EXIT_MAX_ATTEMPTS == 2
