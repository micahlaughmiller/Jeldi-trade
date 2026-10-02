"""Schwab-only: this folder's real (un-pinned) ADOPT_UNTRACKED_SPXW. The shared tests pin it to the Alpaca
default; this guards the value each broker folder actually ships with."""

import config


def test_schwab_adoption_default():
    assert config.ADOPT_UNTRACKED_SPXW is False
