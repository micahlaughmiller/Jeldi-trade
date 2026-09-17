"""Manages the single open SPXW credit spread: stop, target, runner, verified closes."""

import logging
from dataclasses import asdict, dataclass, field
from datetime import date, datetime

import pandas as pd

import config
import strategy
from broker import BrokerError
from journal import Journal

log = logging.getLogger(__name__)


@dataclass
class OpenSpread:
    direction: str
    setup: str
    right: str
    root: str
    expiration: date
    short_strike: float
    long_strike: float
    width: int
    qty: int
    entry_credit: float
    entry_time: datetime
    current_price: float
    best_price: float
    runner: bool = False
    runner_best: float | None = None
    momentum_at_target: float | None = None
    closed_qty: int = 0
    realized_pnl: float = 0.0

    @property
    def remaining(self) -> int:
        return self.qty - self.closed_qty

    @property
    def stop_price(self) -> float:
        return round(self.entry_credit + config.STOP_LOSS, 2)

    @property
    def target_price(self) -> float:
        return round(self.entry_credit - config.PROFIT_TARGET, 2)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["expiration"] = self.expiration.isoformat()
        d["entry_time"] = self.entry_time.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "OpenSpread":
        kw = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        kw["expiration"] = date.fromisoformat(kw["expiration"])
        kw["entry_time"] = datetime.fromisoformat(kw["entry_time"])
        return cls(**kw)


def occ_root(symbol: str) -> str:
    return symbol[:-15] if len(symbol) > 15 else symbol


def spxw_legs(positions: list[dict]) -> list[dict]:
    return [p for p in positions
            if p.get("asset_class") == "option" and occ_root(p["symbol"]) == config.OPTION_ROOT]


class PositionManager:
    def __init__(self, broker, journal: Journal):
        self.broker = broker
        self.journal = journal
        self.position: OpenSpread | None = None

    def open(self, spread: OpenSpread) -> None:
        self.position = spread
        log.info("POSITION OPEN %s %s %dx %s %s/%s credit %.2f stop %.2f target %.2f",
                 spread.setup, spread.direction, spread.qty, spread.right,
                 spread.short_strike, spread.long_strike, spread.entry_credit,
                 spread.stop_price, spread.target_price)
        self.journal.event("ENTRY", spread.entry_time, position=spread.to_dict())

    def adopt_from_broker(self, positions: list[dict], now: datetime,
                          saved: dict | None = None) -> OpenSpread | None:
        legs = spxw_legs(positions)
        groups: dict[tuple, list[dict]] = {}
        for leg in legs:
            groups.setdefault((leg["expiration"], leg["right"]), []).append(leg)
        for (expiration, right), group in groups.items():
            shorts = [p for p in group if p["qty"] < 0]
            longs = [p for p in group if p["qty"] > 0]
            if not shorts or not longs:
                log.error("BROKEN SPREAD at broker: %s", [p["symbol"] for p in group])
                continue
            short, long = shorts[0], longs[0]
            qty = min(abs(short["qty"]), abs(long["qty"]))
            entry_credit = round(short["avg_price"] - long["avg_price"], 2)
            current = entry_credit
            if short.get("current_price") is not None and long.get("current_price") is not None:
                current = round(short["current_price"] - long["current_price"], 2)
            if saved and saved.get("short_strike") == short["strike"] and saved.get("right") == right:
                spread = OpenSpread.from_dict(saved)
                spread.qty = qty + spread.closed_qty
                spread.current_price = current
            else:
                spread = OpenSpread(
                    direction=strategy.BULLISH if right == "P" else strategy.BEARISH,
                    setup="ADOPTED", right=right, root=config.OPTION_ROOT,
                    expiration=expiration if isinstance(expiration, date) else date.fromisoformat(str(expiration)),
                    short_strike=float(short["strike"]), long_strike=float(long["strike"]),
                    width=int(abs(short["strike"] - long["strike"])), qty=qty,
                    entry_credit=entry_credit, entry_time=now,
                    current_price=current, best_price=min(current, entry_credit),
                )
            self.position = spread
            log.warning("ADOPTED broker position: %s", spread.to_dict())
            self.journal.event("ADOPTED", now, position=spread.to_dict())
            return spread
        return None

    def on_tick(self, now: datetime, spread_price: float, candles: pd.DataFrame) -> list[dict]:
        p = self.position
        if p is None:
            return []
        p.current_price = spread_price
        p.best_price = min(p.best_price, spread_price)
        if p.runner:
            return self._runner_tick(now, spread_price, candles)
        if spread_price >= p.stop_price:
            return self._close(p.remaining, "STOP_LOSS", now, spread_price)
        if spread_price <= p.target_price:
            if (config.RUNNER_ENABLED and p.remaining >= config.RUNNER_MIN_CONTRACTS
                    and strategy.momentum_continuing(candles, p.direction)):
                fills = self._close(p.remaining // 2, "TARGET_HALF", now, spread_price)
                if fills and self.position is not None:
                    p.runner = True
                    p.runner_best = spread_price
                    p.momentum_at_target = strategy.momentum(candles, p.direction)
                    log.info("RUNNER MODE: %d left, stop %.2f, trail %.2f, momentum ref %.3f",
                             p.remaining, p.target_price, config.TRAIL_AMOUNT, p.momentum_at_target)
                    self.journal.event("RUNNER_START", now, position=p.to_dict())
                return fills
            return self._close(p.remaining, "PROFIT_TARGET", now, spread_price)
        return []

    def _runner_tick(self, now: datetime, spread_price: float, candles: pd.DataFrame) -> list[dict]:
        p = self.position
        p.runner_best = min(p.runner_best, spread_price)
        reason = None
        if spread_price >= p.target_price:
            reason = "RUNNER_STOP"
        elif spread_price >= p.runner_best + config.TRAIL_AMOUNT:
            reason = "RUNNER_TRAIL"
        elif p.momentum_at_target is not None and p.momentum_at_target > 0 \
                and strategy.momentum_slowed(strategy.momentum(candles, p.direction), p.momentum_at_target):
            reason = "RUNNER_MOMENTUM_SLOWED"
        if reason is None:
            return []
        return self._close(p.remaining, reason, now, spread_price)

    def force_close(self, now: datetime, spread_price: float | None, reason: str = "FORCE_CLOSE") -> list[dict]:
        p = self.position
        if p is None:
            return []
        return self._close(p.remaining, reason, now, spread_price if spread_price is not None else p.current_price)

    def _remaining_at_broker(self) -> int:
        p = self.position
        legs = [leg for leg in spxw_legs(self.broker.get_positions())
                if leg["right"] == p.right and leg["expiration"] == p.expiration
                and leg["strike"] in (p.short_strike, p.long_strike)]
        return max((abs(leg["qty"]) for leg in legs), default=0)

    def _close(self, qty: int, reason: str, now: datetime, spread_price: float) -> list[dict]:
        p = self.position
        if qty <= 0:
            return []
        expected_left = p.remaining - qty
        log.warning("CLOSING %d/%d %s (%s) at market, spread mid %.2f",
                    qty, p.remaining, p.right, reason, spread_price)
        order = None
        left = expected_left
        for attempt in range(1, config.CLOSE_MAX_RETRIES + 1):
            try:
                order = self.broker.close_spread_at_market(
                    config.UNDERLYING, p.expiration, p.right, p.short_strike, p.long_strike, qty,
                    time_in_force="day", root=p.root)
            except BrokerError as e:
                order = None
                log.error("CLOSE_FAILED attempt %d/%d: %s", attempt, config.CLOSE_MAX_RETRIES, e)
                self.journal.event("CLOSE_FAILED", now, attempt=attempt, error=str(e), reason=reason)
                continue
            if order.get("status") == "dry_run":
                break
            left = self._remaining_at_broker()
            if left <= expected_left:
                break
            log.error("CLOSE_FAILED attempt %d/%d: broker still shows %d contracts (expected %d)",
                      attempt, config.CLOSE_MAX_RETRIES, left, expected_left)
            self.journal.event("CLOSE_FAILED", now, attempt=attempt, broker_qty=left,
                               expected=expected_left, reason=reason)
            order = None
        else:
            log.critical("CLOSE_FAILED: %s legs still open after %d attempts -- MANUAL ACTION REQUIRED",
                         p.root, config.CLOSE_MAX_RETRIES)
            return []
        exit_price = order.get("filled_avg_price") if order else None
        exit_price = float(exit_price) if exit_price is not None else spread_price
        fill_qty = p.remaining - left
        pnl = round((p.entry_credit - exit_price) * 100 * fill_qty, 2)
        p.closed_qty += fill_qty
        p.realized_pnl = round(p.realized_pnl + pnl, 2)
        row = {
            "date": now.date().isoformat(), "entry_time": p.entry_time, "exit_time": now,
            "direction": p.direction, "setup": p.setup, "right": p.right,
            "short_strike": p.short_strike, "long_strike": p.long_strike, "width": p.width,
            "qty": fill_qty, "entry_credit": p.entry_credit, "exit_price": exit_price,
            "pnl": pnl, "exit_reason": reason, "runner": "y" if p.runner else "n",
            "position_closed": p.remaining == 0, "position_pnl": p.realized_pnl,
        }
        log.warning("CLOSED %d @ %.2f (%s) pnl %.2f | position realized %.2f, %d left",
                    fill_qty, exit_price, reason, pnl, p.realized_pnl, p.remaining)
        self.journal.event("EXIT", now, **row)
        if p.remaining == 0:
            self.position = None
        return [row]
