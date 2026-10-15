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
