"""Tiering, contract sizing, and daily circuit breakers."""

from dataclasses import asdict, dataclass, field
from math import floor

import config


def tier(equity: float) -> int:
    for ceiling, t in config.TIER_BANDS:
        if equity < ceiling:
            return t
    return config.TIER_BANDS[-1][1]


def contracts_for(equity: float, width: int, credit: float, open_risk_dollars: float,
                  news_day: bool = False) -> int:
    max_loss = (width - credit) * 100.0
    if max_loss <= 0 or equity <= 0:
        return 0
    n = floor(equity * config.RISK_PER_TRADE_PCT / max_loss)
    if n == 0 and config.ALLOW_MIN_CONTRACT_OVERRIDE \
            and (open_risk_dollars + max_loss) / equity <= config.MAX_PORTFOLIO_RISK_PCT:
        n = 1
    portfolio_cap = floor((config.MAX_PORTFOLIO_RISK_PCT * equity - open_risk_dollars) / max_loss)
    n = min(n, max(portfolio_cap, 0), config.MAX_CONTRACTS_PER_TRADE)
    if news_day and config.NEWS_DAY_MODE == "half_size" and n >= 1:
        n = max(1, n // 2)
    return max(n, 0)


@dataclass
class DayState:
    start_equity: float
    trades_today: int = 0
    consecutive_losses: int = 0
    realized_pnl: float = 0.0
    closed_trades: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "DayState":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


class RiskManager:
    def __init__(self, start_equity: float, state: DayState | None = None):
        self.state = state or DayState(start_equity=start_equity)

    def record_trade(self, pnl: float, row: dict | None = None) -> None:
        self.state.trades_today += 1
        self.state.realized_pnl += pnl
        self.state.consecutive_losses = self.state.consecutive_losses + 1 if pnl < 0 else 0
        if row is not None:
            self.state.closed_trades.append(row)

    def daily_loss_limit(self) -> float:
        return -config.DAILY_LOSS_LIMIT_PCT * self.state.start_equity

    def trading_allowed(self, equity: float | None = None) -> tuple[bool, str]:
        s = self.state
        if s.trades_today >= config.MAX_TRADES_PER_DAY:
            return False, f"MAX_TRADES_PER_DAY ({s.trades_today})"
        if s.consecutive_losses >= config.MAX_CONSECUTIVE_LOSSES:
            return False, f"MAX_CONSECUTIVE_LOSSES ({s.consecutive_losses})"
        if s.realized_pnl <= self.daily_loss_limit():
            return False, f"DAILY_LOSS_LIMIT realized={s.realized_pnl:.2f}"
        if equity is not None and equity - s.start_equity <= self.daily_loss_limit():
            return False, f"DAILY_LOSS_LIMIT equity={equity:.2f} start={s.start_equity:.2f}"
        return True, "OK"
