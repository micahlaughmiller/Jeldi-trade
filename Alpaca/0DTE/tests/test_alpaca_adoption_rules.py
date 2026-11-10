"""Alpaca-only: this folder's real (un-pinned) ADOPT_UNTRACKED_SPXW. The shared tests pin it to the Alpaca
default; this guards the value each broker folder actually ships with."""

import config


def test_alpaca_adoption_default():
    assert config.ADOPT_UNTRACKED_SPXW is True
    assert config.ADOPT_UNTRACKED_STOCK is True


def test_the_shipped_open_position_cap_is_two():
    assert config.MAX_OPEN_POSITIONS_TOTAL == 2


def test_the_shipped_daily_limits():
    """Alpaca: 10% daily loss limit, F exempt from it, no profit target (Schwab 0DTE differs on purpose, 2026-10-15)."""
    assert config.DAILY_LOSS_LIMIT_PCT == 0.10 and config.DAILY_LOSS_LIMIT_EXEMPT == ("F",)
    assert config.DAILY_PROFIT_TARGET_PCT is None and config.DAILY_LOSS_BASIS == "equity"


def test_the_shipped_exit_booking_and_resting_exits():
    """2026-11-06: the paper bot keeps the tick-before-the-trigger basis and script-managed exits (Alpaca cannot hold an OCO)."""
    assert config.EXIT_PNL_BASIS == "prev_tick"
    assert config.RESTING_EXITS is False


def test_the_shipped_data_source_and_opening_range():
    """The paper bots keep Yahoo SPX data and the 30-minute opening range."""
    assert config.SPX_CANDLE_SOURCE == "yahoo"
    assert config.OPENING_RANGE_MINUTES == 30 and config.OVERNIGHT_ENTRY_END_MIN == 30
