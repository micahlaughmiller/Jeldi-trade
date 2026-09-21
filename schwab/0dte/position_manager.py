"""Manages the open SPXW credit spreads, one per strategy: stop, target, runner, verified closes."""

import logging
from dataclasses import asdict, dataclass
from datetime import date, datetime

import pandas as pd

import config
import strategy
from broker import BrokerError
from journal import Journal

log = logging.getLogger(__name__)


@dataclass
class OpenSpread:
    strategy: str
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
    profit_target: float
    stop_loss: float
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
        return round(self.entry_credit + self.stop_loss, 2)

    @property
    def target_price(self) -> float:
        return round(self.entry_credit - self.profit_target, 2)

    @property
    def open_risk(self) -> float:
        return (self.width - self.entry_credit) * 100.0 * self.remaining

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


def classify_by_moneyness(right: str, short_strike: float, spot: float) -> str:
    """A sells ITM (put above spot / call below spot), B sells OTM."""
    itm = short_strike > spot if right == "P" else short_strike < spot
    return "A" if itm else "B"


def pair_legs(group: list[dict]) -> tuple[list[tuple[dict, dict]], list[dict]]:
    """Pair short and long legs of one expiration/right into spreads by strike order.

    A and B never overlap (ITM vs OTM), so sorting both sides by strike lines each
    short up with its own long. Returns (pairs, unpaired legs).
    """
    shorts = sorted((p for p in group if p["qty"] < 0), key=lambda p: p["strike"])
    longs = sorted((p for p in group if p["qty"] > 0), key=lambda p: p["strike"])
    n = min(len(shorts), len(longs))
    return list(zip(shorts[:n], longs[:n])), shorts[n:] + longs[n:]


class PositionManager:
    def __init__(self, broker, journal: Journal):
        self.broker = broker
        self.journal = journal
        self.positions: dict[str, OpenSpread] = {}

    def open(self, spread: OpenSpread) -> None:
        self.positions[spread.strategy] = spread
        log.info("POSITION OPEN [%s] %s %s %dx %s %s/%s credit %.2f stop %.2f target %.2f",
                 spread.strategy, spread.setup, spread.direction, spread.qty, spread.right,
                 spread.short_strike, spread.long_strike, spread.entry_credit,
                 spread.stop_price, spread.target_price)
        self.journal.event("ENTRY", spread.entry_time, strategy=spread.strategy, position=spread.to_dict())

    def total_open_risk(self) -> float:
        return sum(p.open_risk for p in self.positions.values())

    def adopt_from_broker(self, positions: list[dict], now: datetime, spot: float,
                          saved: dict[str, dict] | None = None) -> list[OpenSpread]:
        """Adopt broker spreads not already tracked; classify each as A or B."""
        groups: dict[tuple, list[dict]] = {}
        for leg in spxw_legs(positions):
            groups.setdefault((leg["expiration"], leg["right"]), []).append(leg)
        adopted: list[OpenSpread] = []
        for (expiration, right), group in groups.items():
            pairs, broken = pair_legs(group)
            if broken:
                log.error("BROKEN SPREAD at broker: %s", [p["symbol"] for p in broken])
            for short, long in pairs:
                if any(p.right == right and p.short_strike == short["strike"] for p in self.positions.values()):
                    continue
                spread = self._adopt_pair(short, long, expiration, right, now, spot, saved or {})
                if spread is None:
                    continue
                self.positions[spread.strategy] = spread
                adopted.append(spread)
                log.warning("ADOPTED broker position as %s: %s", spread.strategy, spread.to_dict())
                self.journal.event("ADOPTED", now, strategy=spread.strategy, position=spread.to_dict())
        return adopted

    def _adopt_pair(self, short: dict, long: dict, expiration, right: str, now: datetime,
                    spot: float, saved: dict[str, dict]) -> OpenSpread | None:
        qty = min(abs(short["qty"]), abs(long["qty"]))
        entry_credit = round(short["avg_price"] - long["avg_price"], 2)
        current = entry_credit
        if short.get("current_price") is not None and long.get("current_price") is not None:
            current = round(short["current_price"] - long["current_price"], 2)
        match = next((s for s in saved.values()
                      if s and s.get("short_strike") == short["strike"] and s.get("right") == right), None)
        if match:
            spread = OpenSpread.from_dict(match)
            spread.qty = qty + spread.closed_qty
            spread.current_price = current
            return spread
        strat = classify_by_moneyness(right, short["strike"], spot)
        if strat in self.positions:
            free = [s for s in config.STRATEGIES if s not in self.positions]
            if not free:
                log.error("Cannot adopt %s%s/%s: every strategy slot is taken", short["strike"], right, long["strike"])
                return None
            strat = free[0]
        target, stop = strategy.exit_levels(strat)
        return OpenSpread(
            strategy=strat, direction=strategy.BULLISH if right == "P" else strategy.BEARISH,
            setup="ADOPTED", right=right, root=config.OPTION_ROOT,
            expiration=expiration if isinstance(expiration, date) else date.fromisoformat(str(expiration)),
            short_strike=float(short["strike"]), long_strike=float(long["strike"]),
            width=int(abs(short["strike"] - long["strike"])), qty=qty,
            entry_credit=entry_credit, entry_time=now,
            current_price=current, best_price=min(current, entry_credit),
            profit_target=target, stop_loss=stop,
        )

    def on_tick(self, now: datetime, prices: dict[str, float], candles: pd.DataFrame) -> list[dict]:
        fills: list[dict] = []
        for strat, p in list(self.positions.items()):
            if strat in prices:
                fills += self._tick_position(p, now, prices[strat], candles)
        return fills

    def _tick_position(self, p: OpenSpread, now: datetime, spread_price: float, candles: pd.DataFrame) -> list[dict]:
        p.current_price = spread_price
        p.best_price = min(p.best_price, spread_price)
        if p.runner:
            return self._runner_tick(p, now, spread_price, candles)
        if spread_price >= p.stop_price:
            return self._close(p, p.remaining, "STOP_LOSS", now, spread_price)
        if spread_price <= p.target_price:
            if (config.RUNNER_ENABLED and p.remaining >= config.RUNNER_MIN_CONTRACTS
                    and strategy.momentum_continuing(candles, p.direction)):
                fills = self._close(p, p.remaining // 2, "TARGET_HALF", now, spread_price)
                if fills and p.remaining > 0:
                    p.runner = True
                    p.runner_best = spread_price
                    p.momentum_at_target = strategy.momentum(candles, p.direction)
                    log.info("[%s] RUNNER MODE: %d left, stop %.2f, trail %.2f, momentum ref %.3f",
                             p.strategy, p.remaining, p.target_price, config.TRAIL_AMOUNT, p.momentum_at_target)
                    self.journal.event("RUNNER_START", now, strategy=p.strategy, position=p.to_dict())
                return fills
            return self._close(p, p.remaining, "PROFIT_TARGET", now, spread_price)
        if config.PROFIT_LOCK_ENABLED and (p.entry_credit - p.best_price) >= config.PROFIT_LOCK_ARM:
            profit = p.entry_credit - spread_price
            if spread_price >= p.best_price + config.PROFIT_LOCK_GIVEBACK:
                log.info("[%s] PROFIT LOCK: best %.2f, now %.2f (gave back %.2f); locking %+.2f",
                         p.strategy, p.best_price, spread_price, spread_price - p.best_price, profit)
                return self._close(p, p.remaining, "PROFIT_LOCK_GIVEBACK", now, spread_price)
            if config.PROFIT_LOCK_ON_MOMENTUM_FLIP and profit > 0 and strategy.candle_against(candles, p.direction):
                log.info("[%s] PROFIT LOCK: candle closed against the trade with %+.2f open profit",
                         p.strategy, profit)
                return self._close(p, p.remaining, "PROFIT_LOCK_MOMENTUM", now, spread_price)
        return []

    def _runner_tick(self, p: OpenSpread, now: datetime, spread_price: float, candles: pd.DataFrame) -> list[dict]:
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
        return self._close(p, p.remaining, reason, now, spread_price)

    def force_close(self, now: datetime, prices: dict[str, float | None],
                    reason: str = "FORCE_CLOSE") -> list[dict]:
        fills: list[dict] = []
        for strat, p in list(self.positions.items()):
            price = prices.get(strat)
            fills += self._close(p, p.remaining, reason, now, price if price is not None else p.current_price)
        return fills

    def _remaining_at_broker(self, p: OpenSpread) -> int:
        legs = [leg for leg in spxw_legs(self.broker.get_positions())
                if leg["right"] == p.right and leg["expiration"] == p.expiration
                and leg["strike"] in (p.short_strike, p.long_strike)]
        return max((abs(leg["qty"]) for leg in legs), default=0)

    def _close(self, p: OpenSpread, qty: int, reason: str, now: datetime, spread_price: float) -> list[dict]:
        if qty <= 0:
            return []
        expected_left = p.remaining - qty
        log.warning("[%s] CLOSING %d/%d %s (%s) at market, spread mid %.2f",
                    p.strategy, qty, p.remaining, p.right, reason, spread_price)
        order = None
        left = expected_left
        for attempt in range(1, config.CLOSE_MAX_RETRIES + 1):
            try:
                order = self.broker.close_spread_at_market(
                    config.UNDERLYING, p.expiration, p.right, p.short_strike, p.long_strike, qty,
                    time_in_force="day", root=p.root)
            except BrokerError as e:
                order = None
                log.error("[%s] CLOSE_FAILED attempt %d/%d: %s", p.strategy, attempt, config.CLOSE_MAX_RETRIES, e)
                self.journal.event("CLOSE_FAILED", now, strategy=p.strategy, attempt=attempt,
                                   error=str(e), reason=reason)
                continue
            if order.get("status") == "dry_run":
                break
            left = self._remaining_at_broker(p)
            if left <= expected_left:
                break
            log.error("[%s] CLOSE_FAILED attempt %d/%d: broker still shows %d contracts (expected %d)",
                      p.strategy, attempt, config.CLOSE_MAX_RETRIES, left, expected_left)
            self.journal.event("CLOSE_FAILED", now, strategy=p.strategy, attempt=attempt, broker_qty=left,
                               expected=expected_left, reason=reason)
            order = None
        else:
            log.critical("[%s] CLOSE_FAILED: %s legs still open after %d attempts -- MANUAL ACTION REQUIRED",
                         p.strategy, p.root, config.CLOSE_MAX_RETRIES)
            return []
        exit_price = order.get("filled_avg_price") if order else None
        exit_price = float(exit_price) if exit_price is not None else spread_price
        fill_qty = p.remaining - left
        pnl = round((p.entry_credit - exit_price) * 100 * fill_qty, 2)
        p.closed_qty += fill_qty
        p.realized_pnl = round(p.realized_pnl + pnl, 2)
        row = {
            "date": now.date().isoformat(), "strategy": p.strategy,
            "entry_time": p.entry_time, "exit_time": now,
            "direction": p.direction, "setup": p.setup, "right": p.right,
            "short_strike": p.short_strike, "long_strike": p.long_strike, "width": p.width,
            "qty": fill_qty, "entry_credit": p.entry_credit, "exit_price": exit_price,
            "pnl": pnl, "exit_reason": reason, "runner": "y" if p.runner else "n",
            "position_closed": p.remaining == 0, "position_pnl": p.realized_pnl,
            "total_qty": p.qty, "root": p.root,
        }
        log.warning("[%s] CLOSED %d @ %.2f (%s) pnl %.2f | position realized %.2f, %d left",
                    p.strategy, fill_qty, exit_price, reason, pnl, p.realized_pnl, p.remaining)
        self.journal.event("EXIT", now, **row)
        if p.remaining == 0:
            del self.positions[p.strategy]
        return [row]
