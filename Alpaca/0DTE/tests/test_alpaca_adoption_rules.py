"""Alpaca-only: this folder's real (un-pinned) ADOPT_UNTRACKED_SPXW. The shared tests pin it to the Alpaca
default; this guards the value each broker folder actually ships with."""

import config


def test_alpaca_adoption_default():
    assert config.ADOPT_UNTRACKED_SPXW is True
    assert config.ADOPT_UNTRACKED_STOCK is True


def test_the_shipped_open_position_cap_is_two():
    assert config.MAX_OPEN_POSITIONS_TOTAL == 2
