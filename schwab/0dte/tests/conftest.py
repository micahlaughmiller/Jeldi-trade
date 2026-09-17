import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402

ET = config.ET


def et(h: int, m: int, s: int = 0, day: int = 17) -> datetime:
    return datetime(2026, 9, day, h, m, s, tzinfo=ET)


def make_candles(start: datetime, bars: list[tuple[float, float, float, float]], minutes: int = 2) -> pd.DataFrame:
    """bars = [(open, high, low, close), ...] starting at `start`, one per `minutes`."""
    idx = pd.DatetimeIndex([start + timedelta(minutes=minutes * i) for i in range(len(bars))])
    df = pd.DataFrame(bars, columns=["open", "high", "low", "close"], index=idx)
    df["volume"] = 1000
    return df


@pytest.fixture
def now_1000() -> datetime:
    return et(10, 0)
