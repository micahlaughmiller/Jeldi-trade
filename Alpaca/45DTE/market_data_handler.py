"""S&P 500 universe download and Wilder-RSI signal scan."""

from __future__ import annotations

import math
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import numpy as np
import pandas as pd
import yfinance as yf

import config_45dte

HERE = Path(__file__).resolve().parent


@dataclass
class ScanResult:
    results: list[dict[str, Any]] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    elapsed_sec: float = 0.0

    @property
    def ok_count(self) -> int:
        return len(self.results)

    @property
    def signals(self) -> list[dict[str, Any]]:
        return [r for r in self.results if r["signal"] is not None]


def to_yf_symbol(broker_symbol: str) -> str:
    return broker_symbol.replace(".", "-")


def to_broker_symbol(yf_symbol: str) -> str:
    return yf_symbol.replace("-", ".")


def load_universe(path: str | Path | None = None, config: ModuleType = config_45dte) -> list[str]:
    """Broker-spelled tickers (e.g. ``BRK.B``), de-duplicated, order preserved."""
    file = Path(path) if path is not None else Path(config.UNIVERSE_FILE)
    if not file.is_absolute():
        file = HERE / file
    seen: set[str] = set()
    out: list[str] = []
    for line in file.read_text().splitlines():
        sym = line.strip().upper()
        if sym and not sym.startswith("#") and sym not in seen:
            seen.add(sym)
            out.append(sym)
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def rsi_wilder(close: pd.Series, period: int) -> pd.Series:
    """RSI seeded with an SMA of the first `period` changes, then Wilder-smoothed."""
    values = close.to_numpy(dtype=float)
    n = len(values)
    out = np.full(n, np.nan)
    if n <= period:
        return pd.Series(out, index=close.index)
    deltas = np.diff(values)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = float(gains[:period].mean())
    avg_loss = float(losses[:period].mean())
    out[period] = _rsi_value(avg_gain, avg_loss)
    for i in range(period, n - 1):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i + 1] = _rsi_value(avg_gain, avg_loss)
    return pd.Series(out, index=close.index)


def classify(rsi_fast: float, rsi_slow: float, config: ModuleType = config_45dte) -> tuple[str | None, bool]:
    """Return (signal, strong): 'oversold' / 'overbought' / None."""
    if np.isnan(rsi_fast) or np.isnan(rsi_slow):
        return None, False
    if rsi_fast < config.RSI_OVERSOLD and rsi_slow < config.RSI_OVERSOLD:
        strong = rsi_fast < config.RSI_STRONG_OVERSOLD and rsi_slow < config.RSI_STRONG_OVERSOLD
        return "oversold", strong
    if rsi_fast > config.RSI_OVERBOUGHT and rsi_slow > config.RSI_OVERBOUGHT:
        strong = rsi_fast > config.RSI_STRONG_OVERBOUGHT and rsi_slow > config.RSI_STRONG_OVERBOUGHT
        return "overbought", strong
    return None, False


def extract_closes(frame: pd.DataFrame, yf_symbols: list[str]) -> dict[str, pd.Series]:
    """Per-ticker close series from a batched yfinance frame (MultiIndex or flat)."""
    closes: dict[str, pd.Series] = {}
    if frame is None or frame.empty:
        return closes
    if isinstance(frame.columns, pd.MultiIndex):
        # yfinance puts the ticker on level 0 with group_by="ticker", else the field.
        ticker_level = 0 if "Close" in frame.columns.get_level_values(1) else 1
        present = set(frame.columns.get_level_values(ticker_level))
        for sym in yf_symbols:
            if sym not in present:
                continue
            series = frame.xs(sym, axis=1, level=ticker_level).get("Close")
            if series is not None:
                series = series.dropna()
                if not series.empty:
                    closes[sym] = series.astype(float)
    elif "Close" in frame.columns and len(yf_symbols) == 1:
        series = frame["Close"].dropna()
        if not series.empty:
            closes[yf_symbols[0]] = series.astype(float)
    return closes


def _download_chunk(job: tuple[list[str], str]) -> dict[str, pd.Series]:
    yf_symbols, period = job
    frame = yf.download(
        yf_symbols,
        period=period,
        interval="1d",
        group_by="ticker",
        threads=True,
        progress=False,
        auto_adjust=False,
    )
    return extract_closes(frame, yf_symbols)


def download_closes(yf_symbols: list[str], config: ModuleType = config_45dte) -> dict[str, pd.Series]:
    """One batched yf.download per worker PROCESS. yfinance serializes requests inside a process
    (~0.26 s/ticker -> 130 s for 500 names) and concurrent downloads in one process corrupt its
    shared result dict, so chunks go to separate processes."""
    workers = max(1, min(int(config.DOWNLOAD_WORKERS), len(yf_symbols)))
    if workers == 1:
        return _download_chunk((yf_symbols, config.HISTORY_PERIOD))
    size = math.ceil(len(yf_symbols) / workers)
    jobs = [(yf_symbols[i:i + size], config.HISTORY_PERIOD) for i in range(0, len(yf_symbols), size)]
    closes: dict[str, pd.Series] = {}
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for part in pool.map(_download_chunk, jobs):
            closes.update(part)
    return closes


def analyze(broker_symbol: str, close: pd.Series, config: ModuleType = config_45dte) -> dict[str, Any] | None:
    """RSI(14)/RSI(28) on daily closes; the last bar is today's live bar during the session."""
    if len(close) <= config.RSI_SLOW:
        return None
    rsi_fast = float(rsi_wilder(close, config.RSI_FAST).iloc[-1])
    rsi_slow = float(rsi_wilder(close, config.RSI_SLOW).iloc[-1])
    signal, strong = classify(rsi_fast, rsi_slow, config)
    return {
        "symbol": broker_symbol,
        "broker_symbol": broker_symbol,
        "yf_symbol": to_yf_symbol(broker_symbol),
        "signal": signal,
        "strong": strong,
        "rsi14": round(rsi_fast, 2),
        "rsi28": round(rsi_slow, 2),
        "close": round(float(close.iloc[-1]), 4),
        "as_of": close.index[-1].to_pydatetime() if hasattr(close.index[-1], "to_pydatetime") else None,
    }


def scan(tickers: list[str] | None = None, log: Any = None, config: ModuleType = config_45dte) -> ScanResult:
    """One batched download of the universe, then RSI classification per ticker."""
    universe = tickers if tickers is not None else load_universe(config=config)
    yf_symbols = [to_yf_symbol(s) for s in universe]
    started = time.perf_counter()
    if log:
        log.scan_start(len(universe))
    closes = download_closes(yf_symbols, config)
    result = ScanResult()
    for broker_symbol, yf_symbol in zip(universe, yf_symbols):
        series = closes.get(yf_symbol)
        row = analyze(broker_symbol, series, config) if series is not None else None
        if row is None:
            result.failed.append(broker_symbol)
        else:
            result.results.append(row)
    result.elapsed_sec = round(time.perf_counter() - started, 2)
    if log:
        log.scan_result(result.ok_count, len(result.failed), result.elapsed_sec, result.signals, result.failed)
    return result
