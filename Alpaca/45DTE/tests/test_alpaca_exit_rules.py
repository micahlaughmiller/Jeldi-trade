"""Alpaca-only: the exit rules as Alpaca's own (un-pinned) config_45dte.py sets them.

test_order_manager.py pins the original rules so it means the same thing in both broker folders, so on its
own it would not notice Alpaca's real config drifting. This file runs against the real config: Alpaca keeps
the original rules (DTE force-exit at 7, max-loss stop, fixed profit floor). Schwab's deliberately different
rules are guarded by schwab/45DTE/tests/test_schwab_exit_rules.py."""

import config_45dte


def test_alpaca_keeps_the_original_exit_rules():
    assert config_45dte.PROFIT_TARGET_PCT == 0.50
    assert config_45dte.EXIT_DTE_ENABLED is True and config_45dte.EXIT_DTE == 7
    assert config_45dte.MAX_LOSS_EXIT is True
    assert config_45dte.PROFIT_FLOOR_ENABLED is True and config_45dte.PROFIT_FLOOR_MODE == "fixed"
    assert config_45dte.PROFIT_FLOOR_ARM_PCT_OF_TARGET == 0.85 and config_45dte.PROFIT_FLOOR_PCT_OF_MAX == 0.30


def test_alpaca_books_exits_at_the_tick_before_the_close():
    assert config_45dte.EXIT_PNL_BASIS == "prev_tick" and config_45dte.EXIT_PREV_TICK_MAX_AGE_SEC == 300
