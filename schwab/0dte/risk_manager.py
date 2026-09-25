"""Tiering, contract sizing, and daily circuit breakers (per strategy plus combined)."""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from math import floor

import config


def limit(strat: str, name: str, default=None):
    """Per-strategy limit from LIMITS_BY_STRATEGY, else the module-level config value."""
    v = config.LIMITS_BY_STRATEGY.get(strat, {}).get(name)
    return v if v is not None else getattr(config, name, default)


def tier(equity: float) -> int:
    for ceiling, t in config.TIER_BANDS:
        if equity < ceiling:
            return t
    return config.TIER_BANDS[-1][1]


def contracts_for(equity: float, width: int, credit: float, open_risk_dollars: float,
                  news_day: bool = False, half_size: bool = False) -> int:
    """`half_size` is Strategy D (always) and Strategy E (its choppy-day variant) trading at half
    the testing-phase contract cap, per the user's own instruction -- see config.TESTING_MODE."""
    max_loss = (width - credit) * 100.0
    if max_loss <= 0 or equity <= 0:
        return 0
    n = floor(equity * config.RISK_PER_TRADE_PCT / max_loss)
    if n == 0 and config.ALLOW_MIN_CONTRACT_OVERRIDE \
            and (open_risk_dollars + max_loss) / equity <= config.MAX_PORTFOLIO_RISK_PCT:
        n = 1
    portfolio_cap = floor((config.MAX_PORTFOLIO_RISK_PCT * equity - open_risk_dollars) / max_loss)
    if config.TESTING_MODE:
        contract_cap = config.TESTING_HALF_SIZE_MAX_CONTRACTS if half_size else config.TESTING_MAX_CONTRACTS
    else:
        contract_cap = config.MAX_CONTRACTS_PER_TRADE
    n = min(n, max(portfolio_cap, 0), contract_cap)
    if news_day and config.NEWS_DAY_MODE == "half_size" and n >= 1:
        n = max(1, n // 2)
    return max(n, 0)


def _exit_key(row: dict) -> str:
    v = row.get("exit_time")
    return v if isinstance(v, str) else (v.isoformat() if v is not None else "")


@dataclass
class StrategyState:
    trades_today: int = 0
    consecutive_losses: int = 0
    realized_pnl: float = 0.0
    closed_trades: list[dict] = field(default_factory=list)
    last_exit: str | None = None
    last_exit_setup: str | None = None

    def record(self, pnl: float, row: dict | None) -> None:
        self.trades_today += 1
        self.realized_pnl = round(self.realized_pnl + pnl, 2)
        self.consecutive_losses = self.consecutive_losses + 1 if pnl < 0 else 0
        if row is not None:
            self.closed_trades.append(row)
            if row.get("exit_time") is not None:
                self.last_exit = _exit_key(row)
                self.last_exit_setup = row.get("setup")


def _fresh_strategies() -> dict[str, StrategyState]:
    return {s: StrategyState() for s in config.STRATEGIES}


@dataclass
class DayState:
    start_equity: float
    strategies: dict[str, StrategyState] = field(default_factory=_fresh_strategies)

    def for_strategy(self, strat: str) -> StrategyState:
        return self.strategies.setdefault(strat, StrategyState())

    @property
    def trades_today(self) -> int:
        return sum(s.trades_today for s in self.strategies.values())

    @property
    def realized_pnl(self) -> float:
        return round(sum(s.realized_pnl for s in self.strategies.values()), 2)

    @property
    def closed_trades(self) -> list[dict]:
        return sorted((r for s in self.strategies.values() for r in s.closed_trades), key=_exit_key)

    def pnl_by_strategy(self) -> dict[str, float]:
        return {k: s.realized_pnl for k, s in self.strategies.items()}

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "DayState":
        strategies = {
            k: StrategyState(**{f: v[f] for f in StrategyState.__dataclass_fields__ if f in v})
            for k, v in d.get("strategies", {}).items()
        }
        return cls(start_equity=d["start_equity"], strategies=strategies or _fresh_strategies())


class RiskManager:
    def __init__(self, start_equity: float, state: DayState | None = None):
        self.state = state or DayState(start_equity=start_equity)

    def record_trade(self, strat: str, pnl: float, row: dict | None = None) -> None:
        self.state.for_strategy(strat).record(pnl, row)

    def daily_loss_limit(self) -> float:
        return -config.DAILY_LOSS_LIMIT_PCT * self.state.start_equity

    def trading_allowed(self, strat: str, equity: float | None = None,
                        now: datetime | None = None, setup: str | None = None) -> tuple[bool, str]:
        """`setup` is the setup of the entry being considered. The cool-down only blocks re-entry into
        the SAME setup that just exited (2026-09-23: an early ON_BREAK exit was blocking a later, genuinely
        different ORB signal that represented the morning's move continuing into the day) -- if either the
        candidate setup or the last exit's setup is unknown, the cool-down still applies (conservative)."""
        s = self.state.for_strategy(strat)
        if s.trades_today >= limit(strat, "MAX_TRADES_PER_DAY"):
            return False, f"{strat}: MAX_TRADES_PER_DAY ({s.trades_today})"
        if s.consecutive_losses >= limit(strat, "MAX_CONSECUTIVE_LOSSES"):
            return False, f"{strat}: MAX_CONSECUTIVE_LOSSES ({s.consecutive_losses})"
        cooldown = limit(strat, "COOLDOWN_MIN", 0)
        same_setup = setup is None or s.last_exit_setup is None or setup == s.last_exit_setup
        if cooldown and now is not None and s.last_exit and same_setup:
            until = datetime.fromisoformat(s.last_exit) + timedelta(minutes=cooldown)
            if now < until:
                return False, f"{strat}: COOLDOWN until {until.astimezone(config.ET).strftime('%H:%M')}"
        realized = self.state.realized_pnl
        if realized <= self.daily_loss_limit():
            return False, f"DAILY_LOSS_LIMIT realized={realized:.2f}"
        if equity is not None and equity - self.state.start_equity <= self.daily_loss_limit():
            return False, f"DAILY_LOSS_LIMIT equity={equity:.2f} start={self.state.start_equity:.2f}"
        return True, "OK"
