import os
import sys
from datetime import datetime, time, timedelta
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("ACTIVE_TRADER", "ASTRA")

import config  # noqa: E402

ET = config.ET


@pytest.fixture(autouse=True)
def _pin_default_strategies(monkeypatch):
    """The legacy suite asserts exact A/B key sets; ASTRA's persona override adds F, so every test
    starts from the default two (tests that set STRATEGIES themselves run later and win)."""
    monkeypatch.setattr(config, "STRATEGIES", ("A", "B"))


@pytest.fixture
def wind_down_times(monkeypatch):
    """The session end before the 2026-10-05 hard stop (last entry 15:00, force close 15:30), for the tests that
    exercise the NO_NEW_ENTRIES phase -- the shipped config has no such phase (both are 13:00)."""
    monkeypatch.setattr(config, "LAST_ENTRY_TIME", time(15, 0))
    monkeypatch.setattr(config, "FORCE_CLOSE_TIME", time(15, 30))


@pytest.fixture
def pre_f_stop_fix(monkeypatch):
    """F as it behaved before 2026-10-07 (quote gate 12% of width with no stop ratio, stop fires immediately). The
    older F fixtures use wide quotes and tick within seconds of the fill, which the new gate and hold would refuse."""
    monkeypatch.setattr(config, "F_MAX_QUOTE_SPREAD_FRAC", 0.12)
    monkeypatch.setattr(config, "F_MAX_QUOTE_SPREAD_STOP_RATIO", 1000.0)
    monkeypatch.setitem(config.EXIT_TUNING_BY_STRATEGY, "F", {})


GUARD_MODULES = ("test_alpaca_adoption_rules", "test_schwab_adoption_rules")   # run against the REAL config


@pytest.fixture(autouse=True)
def _pin_adoption_default(request, monkeypatch):
    """The shared tests describe the Alpaca default (untracked same-day SPXW legs are adopted); Schwab's config
    turns that off, so pin it -- except in the per-folder guard modules that check each folder's real value."""
    if request.module.__name__ not in GUARD_MODULES:
        monkeypatch.setattr(config, "ADOPT_UNTRACKED_SPXW", True)


@pytest.fixture(autouse=True)
def _no_preflight_sleep(monkeypatch):
    """The preflight re-reads the broker EXTERNAL_PREFLIGHT_RECHECK_SEC seconds after something looks wrong; the
    tests' fake broker answers instantly, so there is nothing to wait for."""
    monkeypatch.setattr(config, "EXTERNAL_PREFLIGHT_RECHECK_SEC", 0)


def et(h: int, m: int, s: int = 0, day: int = 17) -> datetime:
    return datetime(2026, 9, day, h, m, s, tzinfo=ET)


def make_candles(start: datetime, bars: list[tuple[float, float, float, float]], minutes: int = 2,
                 volumes: list[float] | None = None) -> pd.DataFrame:
    """bars = [(open, high, low, close), ...] starting at `start`, one per `minutes`."""
    idx = pd.DatetimeIndex([start + timedelta(minutes=minutes * i) for i in range(len(bars))])
    df = pd.DataFrame(bars, columns=["open", "high", "low", "close"], index=idx)
    df["volume"] = volumes if volumes is not None else 1000
    return df


@pytest.fixture
def now_1000() -> datetime:
    return et(10, 0)
