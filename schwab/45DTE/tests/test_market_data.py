import numpy as np
import pandas as pd
import pytest

import market_data_handler as mdh

# StockCharts' published 14-period RSI worked example (their spreadsheet rounds intermediates, hence atol 0.1).
STOCKCHARTS_CLOSES = [
    44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28,
    46.00, 46.03, 46.41, 46.22, 45.64, 46.21, 46.25, 45.71, 46.45, 45.78, 45.35, 44.03, 44.18, 44.22, 44.57,
    43.42, 42.66, 43.13,
]
STOCKCHARTS_RSI = [
    70.53, 66.32, 66.55, 69.41, 66.36, 57.97, 62.93, 63.26, 56.06, 62.38, 54.71, 50.42, 39.99, 41.46, 41.87,
    45.46, 37.30, 33.08, 37.77,
]


def test_rsi_wilder_matches_published_series():
    rsi = mdh.rsi_wilder(pd.Series(STOCKCHARTS_CLOSES), 14)
    assert rsi.iloc[:14].isna().all()
    np.testing.assert_allclose(rsi.iloc[14:].to_numpy(), STOCKCHARTS_RSI, atol=0.1)


def test_rsi_wilder_is_not_simple_rolling_mean():
    close = pd.Series(STOCKCHARTS_CLOSES)
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    simple = 100 - 100 / (1 + gain / loss)
    wilder = mdh.rsi_wilder(close, 14)
    assert abs(wilder.iloc[-1] - simple.iloc[-1]) > 0.5


def test_rsi_extremes_and_short_series():
    up = pd.Series(np.arange(1, 40, dtype=float))
    assert mdh.rsi_wilder(up, 14).iloc[-1] == pytest.approx(100.0)
    down = pd.Series(np.arange(40, 1, -1, dtype=float))
    assert mdh.rsi_wilder(down, 14).iloc[-1] == pytest.approx(0.0)
    assert mdh.rsi_wilder(pd.Series([1.0, 2.0, 3.0]), 14).isna().all()


@pytest.mark.parametrize("fast, slow, signal, strong", [
    (29.0, 29.9, "oversold", False),
    (24.9, 24.0, "oversold", True),
    (24.0, 26.0, "oversold", False),
    (70.1, 71.0, "overbought", False),
    (76.0, 75.5, "overbought", True),
    (29.0, 31.0, None, False),
    (71.0, 69.0, None, False),
    (50.0, 50.0, None, False),
    (float("nan"), 20.0, None, False),
])
def test_classify_thresholds(fast, slow, signal, strong):
    assert mdh.classify(fast, slow) == (signal, strong)


def test_ticker_mapping_round_trip():
    assert mdh.to_yf_symbol("BRK.B") == "BRK-B"
    assert mdh.to_broker_symbol("BRK-B") == "BRK.B"
    assert mdh.to_yf_symbol("AAPL") == "AAPL"


def test_load_universe_dedupes_and_keeps_broker_spelling(tmp_path):
    f = tmp_path / "u.txt"
    f.write_text("AAPL\nBRK.B\n\naapl\n# comment\nMSFT\n")
    assert mdh.load_universe(f) == ["AAPL", "BRK.B", "MSFT"]


def test_real_universe_file_has_sp500_plus_etfs():
    universe = mdh.load_universe()
    assert len(universe) == 503 + 34 and len(set(universe)) == len(universe)
    assert "BRK.B" in universe
    for etf in ("XLE", "XLK", "GLD", "USO", "SPY", "TLT", "EEM"):
        assert etf in universe


def _synthetic_frame(tickers: list[str], n: int = 60, nan_ticker: str | None = None) -> pd.DataFrame:
    idx = pd.bdate_range("2026-01-05", periods=n)
    cols = pd.MultiIndex.from_product([tickers, ["Open", "High", "Low", "Close", "Adj Close", "Volume"]])
    frame = pd.DataFrame(np.random.default_rng(0).normal(100, 1, (n, len(cols))), index=idx, columns=cols)
    if nan_ticker:
        frame[(nan_ticker, "Close")] = np.nan
    return frame


def test_extract_closes_from_multiindex_frame_with_missing_ticker():
    frame = _synthetic_frame(["AAPL", "BRK-B", "DEAD"], nan_ticker="DEAD")
    closes = mdh.extract_closes(frame, ["AAPL", "BRK-B", "DEAD", "NOTINFRAME"])
    assert set(closes) == {"AAPL", "BRK-B"}
    assert len(closes["AAPL"]) == 60
    assert closes["BRK-B"].dtype == float


def test_extract_closes_handles_field_first_levels():
    frame = _synthetic_frame(["AAPL"]).swaplevel(axis=1)
    closes = mdh.extract_closes(frame, ["AAPL"])
    assert "AAPL" in closes


def test_scan_uses_one_batched_download_per_worker_and_maps_symbols(monkeypatch):
    monkeypatch.setattr(mdh.config_45dte, "DOWNLOAD_WORKERS", 1)
    calls = []

    def fake_download(tickers, **kwargs):
        calls.append((tickers, kwargs))
        n = 80
        idx = pd.bdate_range("2026-01-05", periods=n)
        cols = pd.MultiIndex.from_product([["BRK-B", "AAPL", "DEAD"], ["Close"]])
        frame = pd.DataFrame(index=idx, columns=cols, dtype=float)
        frame[("BRK-B", "Close")] = np.linspace(100, 50, n)
        frame[("AAPL", "Close")] = np.linspace(50, 100, n)
        return frame

    monkeypatch.setattr(mdh.yf, "download", fake_download)
    result = mdh.scan(["BRK.B", "AAPL", "DEAD"])
    assert len(calls) == 1
    assert calls[0][0] == ["BRK-B", "AAPL", "DEAD"]
    assert calls[0][1]["group_by"] == "ticker" and calls[0][1]["progress"] is False
    assert result.failed == ["DEAD"]
    by_symbol = {r["symbol"]: r for r in result.results}
    assert by_symbol["BRK.B"]["broker_symbol"] == "BRK.B"
    assert by_symbol["BRK.B"]["signal"] == "oversold" and by_symbol["BRK.B"]["strong"]
    assert by_symbol["AAPL"]["signal"] == "overbought"
    assert {s["symbol"] for s in result.signals} == {"BRK.B", "AAPL"}
