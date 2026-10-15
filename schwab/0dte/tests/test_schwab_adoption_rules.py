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
