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
def _pin_default_strategies(request, monkeypatch):
    """The legacy suite asserts exact A/B key sets; ASTRA's persona override adds F, so every test
    starts from the default two (tests that set STRATEGIES themselves run later and win). The per-folder guard
    module checks what each folder really ships."""
    if request.module.__name__ != "test_schwab_adoption_rules":
        monkeypatch.setattr(config, "STRATEGIES", ("A", "B"))


REAL_CONFIG_MODULES = ("test_session_limits", "test_alpaca_adoption_rules", "test_schwab_adoption_rules")   # these run against the shipped session/limit values


@pytest.fixture(autouse=True)
def _pin_open_position_cap_off(request, monkeypatch):
    """The shared suite predates the 2026-10-08 cap of 2 open trades per persona; it runs with it off, tests of the cap
    set it themselves and test_session_limits uses the real value."""
    if request.module.__name__ not in REAL_CONFIG_MODULES:
        monkeypatch.setattr(config, "MAX_OPEN_POSITIONS_TOTAL", None)


@pytest.fixture(autouse=True)
def _pin_total_trade_cap_off(request, monkeypatch):
    """The shared suite predates the 2026-10-07 one-cap-for-all-strategies rule (MAX_TRADES_TOTAL_PER_DAY = 5), so it
    runs with the total cap off; tests of the cap set it themselves and test_session_limits uses the real value."""
    if request.module.__name__ not in REAL_CONFIG_MODULES:
        monkeypatch.setattr(config, "MAX_TRADES_TOTAL_PER_DAY", None)


@pytest.fixture(autouse=True)
def _no_shared_snapshots(request, monkeypatch):
    """Unit tests never touch the shared snapshot directory (shared_cache.py); test_shared_cache / test_data_consistency opt in."""
    if request.module.__name__ not in ("test_shared_cache", "test_data_consistency"):
        monkeypatch.setattr(config, "SHARED_DATA_ENABLED", False)


@pytest.fixture(autouse=True)
def _no_live_es(request, monkeypatch):
    """Unit tests never read a real Schwab token or call Schwab, whatever .env / token.json this machine holds: live ES is off
    unless a test turns it on. test_market_data exercises the real token/status code against its own temp files."""
    import market_data
    market_data._ES_LIVE.update(state=None, fail_until=0.0)
    if request.module.__name__ != "test_market_data":
        monkeypatch.setattr(market_data, "_es_token_check", lambda: (None, "unit tests: live ES off"))
    yield
    market_data._ES_LIVE.update(state=None, fail_until=0.0)


@pytest.fixture(autouse=True)
def _no_clock_check(request, monkeypatch):
    """No test asks the internet what time it is (main() would, at start-up); test_clock_check turns the check on with a fake source."""
    if request.module.__name__ != "test_clock_check":
        monkeypatch.setattr(config, "CLOCK_CHECK_ENABLED", False, raising=False)


def _module_level_defaults(*names):
    """What config.py assigns at module level, before PERSONA_OVERRIDES is applied for the ACTIVE_TRADER."""
    import ast
    tree = ast.parse(Path(config.__file__).read_text(encoding="utf-8"))
    return {node.targets[0].id: ast.literal_eval(node.value) for node in tree.body
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in names}


DE_SPEC = _module_level_defaults("D_BAND_TOUCH_PCT_B", "D_EMA_CHOP_THRESHOLD_PTS", "E_USE_TREND_GATE", "E_CONFIRM_CANDLES")


@pytest.fixture(autouse=True)
def _pin_spec_de_thresholds(request, monkeypatch):
    """The shared suite predates ASTRA's loosened D/E thresholds (2026-10-09) and asserts the spec behaviour; it runs with the spec
    values, tests of the shipped override read PERSONA_OVERRIDES / the real config (test_astra_loosened_de)."""
    if request.module.__name__ != "test_astra_loosened_de":
        for name, value in DE_SPEC.items():
            monkeypatch.setattr(config, name, value)


@pytest.fixture
def wind_down_times(monkeypatch):
    """The session end before the 2026-10-05 hard stop (last entry 15:00, force close 15:30), for the tests that
    exercise the NO_NEW_ENTRIES phase with other times than the shipped ones (last entry 12:00, force close 13:00)."""
    monkeypatch.setattr(config, "LAST_ENTRY_TIME", time(15, 0))
    monkeypatch.setattr(config, "FORCE_CLOSE_TIME", time(15, 30))


@pytest.fixture
def f_five_a_day(monkeypatch):
    """F's daily trade limit before 2026-10-07 (5; it is 3 now), for tests of how entries and open positions are
    counted against that limit -- the counting does not depend on the number."""
    monkeypatch.setitem(config.LIMITS_BY_STRATEGY, "F", {**config.LIMITS_BY_STRATEGY["F"], "MAX_TRADES_PER_DAY": 5})


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
        monkeypatch.setattr(config, "ADOPT_UNTRACKED_STOCK", True, raising=False)


@pytest.fixture(autouse=True)
def _pin_daily_limits(request, monkeypatch):
    """The shared tests describe the Alpaca day limits (10% loss limit, F exempt from it, no profit target); Schwab 0DTE ships 1%, nothing
    exempt and a 2% target (2026-10-15), so pin the Alpaca values -- except in the per-folder guard modules that check each folder's real ones."""
    if request.module.__name__ not in GUARD_MODULES:
        monkeypatch.setattr(config, "DAILY_LOSS_LIMIT_PCT", 0.10)
        monkeypatch.setattr(config, "DAILY_LOSS_LIMIT_EXEMPT", ("F",))
        monkeypatch.setattr(config, "DAILY_PROFIT_TARGET_PCT", None, raising=False)
        monkeypatch.setattr(config, "DAILY_LOSS_BASIS", "equity", raising=False)


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


@pytest.fixture(autouse=True)
def _pin_golive_caps_off(request, monkeypatch):
    """The Schwab go-live contract cap (config.HARD_MAX_CONTRACTS, 1 in dry_run/live) is not part of the shared suite, whatever SCHWAB_MODE this
    machine's .env holds; test_golive_caps sets it itself. Alpaca configs have no such setting (raising=False)."""
    if request.module.__name__ != "test_golive_caps":
        monkeypatch.setattr(config, "HARD_MAX_CONTRACTS", None, raising=False)


@pytest.fixture(autouse=True)
def _pin_exit_pnl_basis(request, monkeypatch):
    """The shared suite was written for the paper basis (the official P/L on the tick before the trigger, 2026-10-01) and
    for script-managed exits; the Schwab folder ships EXIT_PNL_BASIS = "fill" and RESTING_EXITS = True (2026-11-06). Pin the
    paper values -- except in the per-folder guard modules, which check each folder's real ones. Tests of the fill basis and
    of the resting exits set them themselves."""
    if request.module.__name__ not in GUARD_MODULES:
        monkeypatch.setattr(config, "EXIT_PNL_BASIS", "prev_tick", raising=False)
        monkeypatch.setattr(config, "RESTING_EXITS", False, raising=False)


@pytest.fixture(autouse=True)
def _pin_data_source_and_opening_range(request, monkeypatch):
    """The shared suite describes the Alpaca paper setup (Yahoo SPX data, a 30-minute opening range); the Schwab folder ships Schwab
    data and a 15-minute range (2026-11-10). Pin the Alpaca values -- except in the per-folder guard modules, which check each
    folder's real ones."""
    if request.module.__name__ not in GUARD_MODULES:
        monkeypatch.setattr(config, "SPX_CANDLE_SOURCE", "yahoo", raising=False)
        monkeypatch.setattr(config, "OPENING_RANGE_MINUTES", 30, raising=False)
        monkeypatch.setattr(config, "OVERNIGHT_ENTRY_END_MIN", 30, raising=False)
