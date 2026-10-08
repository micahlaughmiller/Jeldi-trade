"""Manages the open credit spreads (SPXW for A-E, one stock root per F position): stop, target, runner, verified closes."""

import logging
import time
from pathlib import Path
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta

import pandas as pd

import config
import strategy
from broker import BrokerError
from journal import Journal

log = logging.getLogger(__name__)
HERE = Path(__file__).resolve().parent


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
    width: float
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
    entry_mid: float | None = None   # quote mid when the entry was placed (fill quality)
    floor_price: float | None = None  # hard profit-floor exit (spread price), set by the floor/stale rules
    stop_streak: int = 0              # consecutive ticks at/above the stop (STOP_CONFIRM_TICKS)
    worst_price: float | None = None  # highest spread price seen (MAE); None until the first tick
    entry_bid: float | None = None
    entry_ask: float | None = None
    overrides: dict | None = None     # per-position exit knobs consulted before exit_setting (F: width-scaled)
    scan: dict | None = None          # F: scanner row + break/confirmation snapshot, carried into the ENTRY event
    # 2026-09-29/30: staged exit system -- every strategy (config.STAGED_EXIT_ENABLED).
    stage1_armed: bool = False                # STAGE1_ARM_PCT profit has been reached at least once
    stage2_armed: bool = False                # STAGE2_TARGET_PCT profit has been reached at least once
    confirmed_best_price: float | None = None  # stage-3 trail anchor: a best_price that has HELD for
    best_price_since: datetime | None = None   # STAGE3_CONFIRM_SECONDS before the trail ratchets to it
    # 2026-09-30: diagnostic instrumentation for a live anomaly (ASTRA reported 5.21 one tick, exited
    # 6.10 the very next -- too large a jump for the real underlying move that second, every persona
    # affected since this is shared position_manager code). current_price AS OF THE PRIOR TICK, so
    # _close can journal what P/L would have been at last quote alongside the real exit -- compare the
    # two while the real root cause (stale/bad quote source feeding spread_price, most likely) is dug
    # into separately; not itself a behavior change to any exit decision.
    prev_price: float | None = None
    # 2026-09-30: quote-jump sanity gate (config.QUOTE_JUMP_SANITY_PCT) -- the actual fix for the
    # anomaly above. last_tick_at is when current_price was last ACCEPTED (used to scale the sanity
    # bound by elapsed time, so F's slower per-ticker cadence isn't held to the same per-second band as
    # the 1-second SPX strategies); pending_quote/_since track an unconfirmed large jump awaiting a
    # second, consistent reading before _tick_position acts on it.
    last_tick_at: datetime | None = None
    pending_quote: float | None = None
    pending_quote_since: datetime | None = None
    # When THIS position first became unable to progress (the first tick that failed the sanity
    # bound), regardless of how many different pending_quote values it's cycled through since --
    # last_tick_at is NOT usable for this: for F it's naturally ~F_PRICE_EVERY_SEC stale even under
    # completely normal operation, which would make QUOTE_JUMP_MAX_HOLD_SECONDS fire immediately on
    # every single F tick. Cleared the moment a value is accepted (immediate, confirmed, or forced).
    stuck_since: datetime | None = None
    # 2026-10-02: manual-override safety (see PositionManager.sync_external). manual_attention freezes all
    # automated closing of a position whose legs at the broker no longer match it (a naked leg after a
    # hand-closed leg); sync_obs/sync_obs_count count consecutive syncs seeing the same wrong picture.
    manual_attention: bool = False
    sync_obs: str | None = None
    sync_obs_count: int = 0
    # 2026-10-07: when the price first sat at/above the stop in the current run (STOP_CONFIRM_TIME_SEC); None when it is below
    stop_first_at: datetime | None = None
    # 2026-10-08: a close that failed on every attempt (see config.CLOSE_BLOCK_BACKOFF_SEC): how many passes in a row, and
    # when the next ordinary attempt may be made
    close_fail_count: int = 0
    close_blocked_until: datetime | None = None
    # 2026-10-08: when a price for this position last arrived (any tick, even one the quote-jump gate then held), and when the
    # prior tick (prev_price) was taken -- the staleness watchdog and the official-exit-price age limit read these
    last_mark_at: datetime | None = None
    prev_price_at: datetime | None = None

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

    @property
    def key(self) -> str:
        """Dict key in PositionManager.positions: the bare letter for A-E, letter:ROOT for stock strategies."""
        if self.strategy in config.STOCK_STRATEGIES:
            return f"{self.strategy}:{self.root}"
        return self.strategy

    @property
    def ticker(self) -> str | None:
        return None if self.root == config.OPTION_ROOT else self.root

    @property
    def underlying(self) -> str:
        return config.UNDERLYING if self.root == config.OPTION_ROOT else self.root

    @property
    def mfe(self) -> float:
        return round(self.entry_credit - self.best_price, 2)

    @property
    def mae(self) -> float:
        worst = self.entry_credit if self.worst_price is None else self.worst_price
        return round(worst - self.entry_credit, 2)

    @property
    def unrealized_pnl(self) -> float:
        return round((self.entry_credit - self.current_price) * 100.0 * self.remaining, 2)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["expiration"] = self.expiration.isoformat()
        d["entry_time"] = self.entry_time.isoformat()
        d["best_price_since"] = self.best_price_since.isoformat() if self.best_price_since else None
        d["last_tick_at"] = self.last_tick_at.isoformat() if self.last_tick_at else None
        d["pending_quote_since"] = self.pending_quote_since.isoformat() if self.pending_quote_since else None
        d["stuck_since"] = self.stuck_since.isoformat() if self.stuck_since else None
        d["stop_first_at"] = self.stop_first_at.isoformat() if self.stop_first_at else None
        d["close_blocked_until"] = self.close_blocked_until.isoformat() if self.close_blocked_until else None
        d["last_mark_at"] = self.last_mark_at.isoformat() if self.last_mark_at else None
        d["prev_price_at"] = self.prev_price_at.isoformat() if self.prev_price_at else None
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "OpenSpread":
        kw = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        kw["expiration"] = date.fromisoformat(kw["expiration"])
        kw["entry_time"] = datetime.fromisoformat(kw["entry_time"])
        if kw.get("best_price_since"):
            kw["best_price_since"] = datetime.fromisoformat(kw["best_price_since"])
        if kw.get("last_tick_at"):
            kw["last_tick_at"] = datetime.fromisoformat(kw["last_tick_at"])
        if kw.get("pending_quote_since"):
            kw["pending_quote_since"] = datetime.fromisoformat(kw["pending_quote_since"])
        if kw.get("stuck_since"):
            kw["stuck_since"] = datetime.fromisoformat(kw["stuck_since"])
        if kw.get("stop_first_at"):
            kw["stop_first_at"] = datetime.fromisoformat(kw["stop_first_at"])
        if kw.get("close_blocked_until"):
            kw["close_blocked_until"] = datetime.fromisoformat(kw["close_blocked_until"])
        for name in ("last_mark_at", "prev_price_at"):
            if kw.get(name):
                kw[name] = datetime.fromisoformat(kw[name])
        return cls(**kw)


def occ_root(symbol: str) -> str:
    return symbol[:-15] if len(symbol) > 15 else symbol


def occ_expiration(symbol: str) -> date | None:
    """The expiration encoded in an OCC option symbol (root + yymmdd + C/P + 8-digit strike), else None."""
    try:
        return datetime.strptime(symbol[-15:-9], "%y%m%d").date()
    except ValueError:
        return None


def option_legs(positions: list[dict], roots: set[str] | None = None) -> list[dict]:
    """Option legs at the broker whose OCC root is in `roots` (None = every option leg)."""
    return [p for p in positions
            if p.get("asset_class") == "option" and (roots is None or occ_root(p["symbol"]) in roots)]


def spxw_legs(positions: list[dict]) -> list[dict]:
    return option_legs(positions, {config.OPTION_ROOT})


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
        self._last_sync: datetime | None = None
        self._hold_logged: datetime | None = None
        self._latch_logged: dict[str, datetime] = {}

    def open(self, spread: OpenSpread) -> str:
        key = self._store(spread)
        log.info("POSITION OPEN [%s] %s %s %dx %s %s/%s credit %.2f stop %.2f target %.2f",
                 key, spread.setup, spread.direction, spread.qty, spread.right,
                 spread.short_strike, spread.long_strike, spread.entry_credit,
                 spread.stop_price, spread.target_price)
        self.journal.event("ENTRY", spread.entry_time, strategy=spread.strategy, position=spread.to_dict())
        return key

    def _store(self, spread: OpenSpread) -> str:
        key = spread.key
        if key in self.positions:
            n = 2
            while f"{key}#{n}" in self.positions:
                n += 1
            tracked_as = f"{key}#{n}"
            log.critical("DUPLICATE POSITION KEY %s: tracking the new spread as %s -- investigate", key, tracked_as)
            self.journal.event("DUPLICATE_POSITION_KEY", spread.entry_time, strategy=spread.strategy, key=key,
                               tracked_as=tracked_as, position=spread.to_dict())
            key = tracked_as
        self.positions[key] = spread
        return key

    def key_of(self, p: OpenSpread) -> str:
        return next((k for k, v in self.positions.items() if v is p), p.key)

    def total_open_risk(self) -> float:
        return sum(p.open_risk for p in self.positions.values())

    def open_count(self, strat: str) -> int:
        return sum(1 for p in self.positions.values() if p.strategy == strat)

    def has_root(self, root: str) -> bool:
        return any(p.root == root for p in self.positions.values())

    def unrealized_pnl(self, strategies: tuple[str, ...]) -> float:
        return round(sum(p.unrealized_pnl for p in self.positions.values() if p.strategy in strategies), 2)

    def open_pnl(self, strategies: tuple[str, ...]) -> float:
        """Everything broker equity already holds for these strategies' OPEN positions that the day's
        realized total does not: the marks on the remaining contracts plus the P/L a partial close
        (runner TARGET_HALF) booked on the position itself. RiskManager only learns a position's P/L
        when it closes in full, so this is what the daily-loss exemption has to add back meanwhile."""
        return round(sum(p.unrealized_pnl + p.realized_pnl for p in self.positions.values()
                         if p.strategy in strategies), 2)

    def find_collision(self, root: str, expiration: date, right: str, short_strike: float,
                       long_strike: float) -> str | None:
        """Key of an open position on exactly these contracts, else None."""
        return next((k for k, p in self.positions.items()
                     if p.root == root and p.expiration == expiration and p.right == right
                     and p.short_strike == short_strike and p.long_strike == long_strike), None)

    def adopt_from_broker(self, positions: list[dict], now: datetime, spot: float,
                          saved: dict[str, dict] | None = None,
                          roots: set[str] | None = None) -> list[OpenSpread]:
        """Adopt broker spreads not already tracked: SPXW pairs as A or B, any other root as F."""
        groups: dict[tuple, list[dict]] = {}
        for leg in option_legs(positions, roots):
            groups.setdefault((occ_root(leg["symbol"]), leg["expiration"], leg["right"]), []).append(leg)
        adopted: list[OpenSpread] = []
        for (root, expiration, right), group in groups.items():
            pairs, broken = pair_legs(group)
            if broken:
                log.error("BROKEN SPREAD at broker: %s", [p["symbol"] for p in broken])
            for short, long in pairs:
                if any(p.root == root and p.right == right and p.expiration == expiration
                       and p.short_strike == short["strike"] for p in self.positions.values()):
                    continue
                spread = self._adopt_pair(short, long, root, expiration, right, now, spot, saved or {})
                if spread is None:
                    continue
                key = self._store(spread)
                adopted.append(spread)
                log.warning("ADOPTED broker position as %s: %s", key, spread.to_dict())
                self.journal.event("ADOPTED", now, strategy=spread.strategy, position=spread.to_dict())
        return adopted

    def _adopt_pair(self, short: dict, long: dict, root: str, expiration, right: str, now: datetime,
                    spot: float, saved: dict[str, dict]) -> OpenSpread | None:
        exp = expiration if isinstance(expiration, date) else date.fromisoformat(str(expiration))
        qty = min(abs(short["qty"]), abs(long["qty"]))
        entry_credit = round(short["avg_price"] - long["avg_price"], 2)
        current = entry_credit
        if short.get("current_price") is not None and long.get("current_price") is not None:
            current = round(short["current_price"] - long["current_price"], 2)
        match = next((s for s in saved.values()
                      if s and s.get("short_strike") == short["strike"] and s.get("right") == right
                      and s.get("root", config.OPTION_ROOT) == root and s.get("expiration") == exp.isoformat()), None)
        if match:
            spread = OpenSpread.from_dict(match)
            saved_remaining = max(int(match.get("qty", 0)) - int(match.get("closed_qty", 0)), 0)
            # the broker may hold MORE than the bot's own saved contracts on these strikes (the owner added some
            # by hand): the bot only ever re-adopts what it opened itself
            spread.qty = (min(qty, saved_remaining) if saved_remaining else qty) + spread.closed_qty
            spread.current_price = current
            return spread
        direction = strategy.BULLISH if right == "P" else strategy.BEARISH
        if root != config.OPTION_ROOT:
            width = float(abs(short["strike"] - long["strike"]))
            ex = strategy.f_exit_params(width)
            strat = config.STOCK_STRATEGIES[0]
            log.error("ADOPTED untracked %s option spread %s%s/%s as %s with width-scaled exits (%.2f/%.2f)",
                      root, short["strike"], right, long["strike"], strat, ex["profit_target"], ex["stop_loss"])
            return OpenSpread(
                strategy=strat, direction=direction, setup="ADOPTED", right=right, root=root, expiration=exp,
                short_strike=float(short["strike"]), long_strike=float(long["strike"]), width=width, qty=qty,
                entry_credit=entry_credit, entry_time=now, current_price=current,
                best_price=min(current, entry_credit), profit_target=ex["profit_target"],
                stop_loss=ex["stop_loss"], overrides=ex["overrides"],
            )
        strat = classify_by_moneyness(right, short["strike"], spot)
        if strat in self.positions:
            free = [s for s in strategy.spx_strategies() if s not in self.positions]
            if not free:
                log.error("Cannot adopt %s%s/%s: every strategy slot is taken", short["strike"], right, long["strike"])
                return None
            strat = free[0]
        target, stop = strategy.exit_levels(strat)
        return OpenSpread(
            strategy=strat, direction=direction, setup="ADOPTED", right=right, root=config.OPTION_ROOT,
            expiration=exp, short_strike=float(short["strike"]), long_strike=float(long["strike"]),
            width=int(abs(short["strike"] - long["strike"])), qty=qty,
            entry_credit=entry_credit, entry_time=now,
            current_price=current, best_price=min(current, entry_credit),
            profit_target=target, stop_loss=stop,
        )

    def on_tick(self, now: datetime, prices: dict[str, float], candles: pd.DataFrame,
                candles_by_key: dict[str, pd.DataFrame] | None = None) -> list[dict]:
        """`candles` is the shared SPX frame for A-E; a stock position only ever sees its own ticker's
        frame from `candles_by_key` (None when it has none yet, which the momentum rules tolerate)."""
        fills: list[dict] = []
        for key, p in list(self.positions.items()):
            if key not in prices:
                continue
            p.last_mark_at = now
            c = (candles_by_key or {}).get(key) if p.strategy in config.STOCK_STRATEGIES else candles
            fills += self._tick_position(p, now, prices[key], c)
        return fills

    def _knob(self, p: OpenSpread, name: str, default=None):
        """Exit knob for one position: its own overrides first, then the strategy's exit_setting."""
        if p.overrides and p.overrides.get(name) is not None:
            return p.overrides[name]
        return strategy.exit_setting(p.strategy, name, default)

    def _sanitize_quote(self, p: OpenSpread, now: datetime, spread_price: float) -> float | None:
        """Guard against a single bad/stale option-chain print reaching any exit decision (config.py's
        quote-jump-sanity section has the full rationale -- a real live anomaly, ASTRA reported 5.21
        one tick then exited ~6.10 the next). Returns the price to act on this tick, or None to sit
        this tick out entirely (p.current_price/prev_price/etc. stay untouched, same as the existing
        "no chain quote this tick" skip in scheduler.spread_prices).

        A jump within QUOTE_JUMP_SANITY_PCT of width (floored at QUOTE_JUMP_SANITY_FLOOR), scaled by a
        FIXED per-strategy expected polling interval (F's much slower F_PRICE_EVERY_SEC, everyone
        else's TICK_SECONDS -- capped at QUOTE_JUMP_MAX_NOMINAL_TICKS so F's real 15x-ish cadence gap
        doesn't inflate the bound to where it stops meaning anything on a thin/cheap width), passes
        straight through. 2026-09-30, post-review: this scaling is deliberately NOT based on how long
        a quote has actually been held pending -- an earlier version measured real elapsed time since
        the last accepted price, which meant the bound kept widening open the longer a bad reading
        went unconfirmed, silently accepting an unconfirmed value once enough wall-clock time had
        passed rather than ever actually requiring a second, consistent reading. Fixed cadence-only
        scaling means the ONLY way a big jump is trusted is the explicit paths below.

        A jump over the bound is held as "pending" until either a second, similarly-sized reading
        confirms it's real (QUOTE_JUMP_CONFIRM_SECONDS held), or a different reading arrives and
        restarts the confirmation clock on that instead. QUOTE_JUMP_MAX_HOLD_SECONDS is a separate,
        explicit backstop measured from p.stuck_since (when THIS position first became unable to
        progress, regardless of how many different pending_quote values it's cycled through since --
        deliberately NOT last_tick_at, which for F is naturally ~F_PRICE_EVERY_SEC stale even under
        normal operation and would fire this on every single F tick): a position must never be stuck
        refusing to tick forever just because a genuinely fast, continuously-evolving REAL move never
        settles on one repeated reading long enough to satisfy the confirm path above -- past that
        ceiling the current reading is forced through regardless, logged distinctly (QUOTE_JUMP_FORCED)
        so a persistently noisy feed shows up rather than silently degrading protection.
        QUOTE_JUMP_SANITY_PCT=None (or 0) disables the gate entirely for that strategy.

        2026-10-01: an ADVERSE move (price rising -- less profit) on a position that already has
        profit protection armed (p.stage1_armed/stage2_armed) skips the hold entirely, unconditionally
        -- see the inline comment above `armed_and_adverse` for why holding would otherwise undermine
        the very floor/trail the staged exit system depends on to react quickly.
        """
        if p.last_tick_at is None:
            p.last_tick_at = now
            return spread_price   # first tick ever for this position -- nothing to compare against
        pct = self._knob(p, "QUOTE_JUMP_SANITY_PCT", None)
        if not pct:
            p.pending_quote = None
            p.pending_quote_since = None
            p.stuck_since = None
            p.last_tick_at = now
            return spread_price
        floor = self._knob(p, "QUOTE_JUMP_SANITY_FLOOR", 0.0)
        expected_interval = config.F_PRICE_EVERY_SEC if p.strategy in config.STOCK_STRATEGIES else config.TICK_SECONDS
        raw_nominal_ticks = expected_interval / max(config.TICK_SECONDS, 1e-9)
        max_nominal_ticks = self._knob(p, "QUOTE_JUMP_MAX_NOMINAL_TICKS", 1.0)
        nominal_ticks = max(1.0, min(raw_nominal_ticks, max_nominal_ticks))
        max_jump = max(floor, pct * p.width) * nominal_ticks
        jump = abs(spread_price - p.current_price)
        # 2026-10-01: once a position has ANY profit protection armed (stage1/stage2's STAGED_EXIT
        # fields), an ADVERSE move (spread_price rising -- less profit, more loss) must never be held
        # for confirmation: a floor/trail exists specifically to react fast to exactly this, and
        # delaying it works directly against that design. Real live symptom this fixes: a position
        # 30+ cents in profit (stage2 armed, a $0.25 floor already active) closing for a LOSS 5
        # seconds to 2 minutes later -- the floor never got a chance to catch the real reversal
        # because this gate was still holding the tick pending confirmation while real price kept
        # sliding past the floor (and the raw stop) during the hold. A bad print causing an early,
        # unnecessary floor/trail exit costs far less than a genuine reversal MISSING the floor it was
        # supposed to be caught by -- the floor/trail price is already the system's own pre-committed
        # "this is an acceptable exit" level, unlike the original anomaly this gate was built for (no
        # protection had armed yet, so a bad-print exit had no floor to fall back on at all).
        armed_and_adverse = (p.stage1_armed or p.stage2_armed) and spread_price > p.current_price
        if jump <= max_jump or armed_and_adverse:
            p.pending_quote = None
            p.pending_quote_since = None
            p.stuck_since = None
            p.last_tick_at = now
            return spread_price
        # How long THIS position has been unable to progress, regardless of how many different
        # pending_quote values it's cycled through since -- NOT last_tick_at (which for F is
        # naturally ~F_PRICE_EVERY_SEC stale even under completely normal operation, and would make
        # this fire on every single F tick).
        if p.stuck_since is None:
            p.stuck_since = now
        held_sec = (now - p.stuck_since).total_seconds()
        max_hold = self._knob(p, "QUOTE_JUMP_MAX_HOLD_SECONDS", None)
        if max_hold is not None and held_sec >= max_hold:
            log.warning("[%s] Quote jump FORCED after %ds stuck (max hold %ds): %.2f -> %.2f (%+.2f, bound %.2f)",
                       p.key, held_sec, max_hold, p.current_price, spread_price,
                       spread_price - p.current_price, max_jump)
            self.journal.event("QUOTE_JUMP_FORCED", now, strategy=p.strategy, from_price=p.current_price,
                               to_price=spread_price, jump=round(jump, 2), max_jump=round(max_jump, 2),
                               held_sec=round(held_sec, 1))
            p.pending_quote = None
            p.pending_quote_since = None
            p.stuck_since = None
            p.last_tick_at = now
            return spread_price
        confirm_sec = self._knob(p, "QUOTE_JUMP_CONFIRM_SECONDS", 0)
        if p.pending_quote is not None and abs(spread_price - p.pending_quote) <= max_jump \
                and p.pending_quote_since is not None \
                and (now - p.pending_quote_since).total_seconds() >= confirm_sec:
            log.warning("[%s] Quote jump CONFIRMED: %.2f -> %.2f (%+.2f, bound %.2f) held %ds -- accepting.",
                       p.key, p.current_price, spread_price, spread_price - p.current_price, max_jump,
                       (now - p.pending_quote_since).total_seconds())
            self.journal.event("QUOTE_JUMP_CONFIRMED", now, strategy=p.strategy, from_price=p.current_price,
                               to_price=spread_price, jump=round(jump, 2), max_jump=round(max_jump, 2))
            p.pending_quote = None
            p.pending_quote_since = None
            p.stuck_since = None
            p.last_tick_at = now
            return spread_price
        if p.pending_quote is None or abs(spread_price - p.pending_quote) > max_jump:
            p.pending_quote = spread_price
            p.pending_quote_since = now
            log.warning("[%s] Quote jump HELD for confirmation: %.2f -> %.2f (%+.2f, bound %.2f)",
                       p.key, p.current_price, spread_price, spread_price - p.current_price, max_jump)
            self.journal.event("QUOTE_JUMP_HELD", now, strategy=p.strategy, from_price=p.current_price,
                               to_price=spread_price, jump=round(jump, 2), max_jump=round(max_jump, 2))
        return None

    def _set_floor(self, p: OpenSpread, now: datetime, profit_floor: float, why: str) -> None:
        floor_price = round(p.entry_credit - profit_floor, 2)
        if p.floor_price is not None and p.floor_price <= floor_price:
            return
        p.floor_price = floor_price
        log.info("[%s] PROFIT FLOOR set at %.2f (+%.2f) by %s", p.key, floor_price, profit_floor, why)
        self.journal.event("FLOOR_SET", now, strategy=p.strategy, floor_price=floor_price, profit_floor=profit_floor,
                           why=why, position=p.to_dict())

    def _update_floor(self, p: OpenSpread, now: datetime, spread_price: float) -> None:
        profit = p.entry_credit - spread_price
        best_profit = p.entry_credit - p.best_price
        steps = strategy.exit_setting(p.strategy, "PROFIT_FLOOR_BY_WIDTH", {}) or {}
        if p.width in steps:
            arm, floor = steps[p.width]
            if round(best_profit, 2) >= arm:
                self._set_floor(p, now, floor, f"PROFIT_FLOOR arm +{arm:.2f}")
        timer = strategy.exit_setting(p.strategy, "STALE_TIMER_MIN")
        if timer and not p.runner and profit > 0 and (now - p.entry_time) >= timedelta(minutes=timer):
            self._set_floor(p, now, strategy.exit_setting(p.strategy, "STALE_FLOOR", 0.05), f"STALE_TIMER {timer}m")

    def _tick_position(self, p: OpenSpread, now: datetime, spread_price: float, candles: pd.DataFrame | None) -> list[dict]:
        tick_before_at = p.last_tick_at     # when the price we are about to call "the tick before" was accepted
        spread_price = self._sanitize_quote(p, now, spread_price)
        if spread_price is None:
            return []   # unconfirmed quote jump -- sit this tick out entirely, see _sanitize_quote
        p.prev_price = p.current_price
        p.prev_price_at = tick_before_at
        p.current_price = spread_price
        if p.worst_price is None or spread_price > p.worst_price:
            p.worst_price = spread_price
        if spread_price < p.best_price:
            p.best_price = spread_price
            p.best_price_since = now
            log.info("[%s] BEST PRICE %.2f (entry %.2f, +%.2f)", p.key, spread_price,
                     p.entry_credit, p.entry_credit - spread_price, extra={"keep": True})
            self.journal.event("BEST_PRICE", now, strategy=p.strategy, best_price=spread_price,
                               entry_credit=p.entry_credit)
        self._update_floor(p, now, spread_price)
        if p.floor_price is not None and spread_price >= p.floor_price and spread_price > p.target_price:
            return self._close(p, p.remaining, "PROFIT_FLOOR", now, spread_price)
        if strategy.exit_setting(p.strategy, "STAGED_EXIT_ENABLED", False):
            return self._staged_tick(p, now, spread_price, candles)
        if p.runner:
            return self._runner_tick(p, now, spread_price, candles)
        # 2026-09-29: time-based, not a raw tick count -- STOP_CONFIRM_TICKS=2 meant ~30s of real
        # confirmation back when TICK_SECONDS was 15, but silently became ~2s once TICK_SECONDS
        # dropped to 1 (a real, unintended regression: A's 0% win rate one week traced partly to
        # this). STOP_CONFIRM_SECONDS names the real-world window directly; the number of ticks
        # needed to satisfy it is derived from the CURRENT tick rate, so it can never silently drift
        # again if TICK_SECONDS changes.
        confirm_seconds = strategy.exit_setting(p.strategy, "STOP_CONFIRM_SECONDS", 0)
        confirm = max(1, round(confirm_seconds / config.TICK_SECONDS))
        if spread_price >= p.stop_price:
            p.stop_streak += 1
            if p.stop_streak >= confirm:
                return self._close(p, p.remaining, "STOP_LOSS", now, spread_price)
            log.info("[%s] STOP_LOSS candidate tick %d/%d (%ds) at %.2f (stop %.2f): holding for confirmation",
                     p.key, p.stop_streak, confirm, confirm_seconds, spread_price, p.stop_price)
            return []
        p.stop_streak = 0
        if spread_price <= p.target_price:
            fraction = config.RUNNER_CLOSE_FRACTION_BY_STRATEGY.get(p.strategy, 0.5)
            book = int(p.remaining * fraction)
            enough = fraction == 0 or p.remaining >= config.RUNNER_MIN_CONTRACTS
            gate = strategy.exit_setting(p.strategy, "RUNNER_MOMENTUM_GATE", True)
            continuing = strategy.momentum_continuing(candles, p.direction) if gate else True
            if config.RUNNER_ENABLED and enough and continuing:
                fills = self._close(p, book, "TARGET_HALF", now, spread_price) if book > 0 else []
                if p.remaining > 0 and (book == 0 or fills):
                    p.runner = True
                    p.runner_best = spread_price
                    p.momentum_at_target = strategy.momentum(candles, p.direction)
                    log.info("[%s] RUNNER MODE: %d left, stop %.2f, trail %.2f, momentum ref %.3f",
                             p.key, p.remaining, p.target_price, self._knob(p, "TRAIL_AMOUNT", config.TRAIL_AMOUNT),
                             p.momentum_at_target)
                    self.journal.event("RUNNER_START", now, strategy=p.strategy, position=p.to_dict())
                return fills
            return self._close(p, p.remaining, "PROFIT_TARGET", now, spread_price)
        lock_on = strategy.exit_setting(p.strategy, "PROFIT_LOCK_ENABLED", True)
        arm = self._knob(p, "PROFIT_LOCK_ARM")
        # Prices and knobs are 2-dp quantities: compare them at 2 dp, or an exact-tie move (0.15 - 0.10
        # = 0.04999...) misses a 0.05 arm and the lock fires one tick late (or never, on a $1-wide F).
        if lock_on and round(p.entry_credit - p.best_price, 2) >= arm:
            profit = p.entry_credit - spread_price
            giveback = self._knob(p, "PROFIT_LOCK_GIVEBACK")
            if spread_price >= round(p.best_price + giveback, 2):
                log.info("[%s] PROFIT LOCK: best %.2f, now %.2f (gave back %.2f); locking %+.2f",
                         p.key, p.best_price, spread_price, spread_price - p.best_price, profit)
                return self._close(p, p.remaining, "PROFIT_LOCK_GIVEBACK", now, spread_price)
            on_flip = strategy.exit_setting(p.strategy, "PROFIT_LOCK_ON_MOMENTUM_FLIP", True)
            if on_flip and profit > 0 and strategy.candle_against(candles, p.direction):
                log.info("[%s] PROFIT LOCK: candle closed against the trade with %+.2f open profit",
                         p.key, profit)
                return self._close(p, p.remaining, "PROFIT_LOCK_MOMENTUM", now, spread_price)
        return []

    def _staged_tick(self, p: OpenSpread, now: datetime, spread_price: float,
                     candles: pd.DataFrame | None) -> list[dict]:
        """2026-09-29/30: staged exit progression -- every strategy uses this (config.
        STAGED_EXIT_ENABLED), replacing the flat stop/target/lock/runner sequence in _tick_position
        above. See config.py's staged-exit section for the four dollar levels and the rationale.
        """
        width = p.width

        def level(pct_name: str) -> float:
            return max(config.STAGE_EXIT_FLOOR, self._knob(p, pct_name) * width)

        confirm_seconds = strategy.exit_setting(p.strategy, "STOP_CONFIRM_SECONDS", 0)
        confirm = max(1, round(confirm_seconds / config.TICK_SECONDS))
        stop_price = round(p.entry_credit + level("STAGE0_STOP_PCT"), 2)
        # 2026-10-07: F's stop waits out the quote noise right after the fill (STOP_MIN_HOLD_SEC, with an emergency stop at
        # STOP_HOLD_EMERGENCY_MULT x the distance) and must hold for STOP_CONFIRM_TIME_SEC of wall-clock time. All 0 elsewhere.
        hold_sec = strategy.exit_setting(p.strategy, "STOP_MIN_HOLD_SEC", 0) or 0
        time_confirm = strategy.exit_setting(p.strategy, "STOP_CONFIRM_TIME_SEC", 0) or 0
        in_hold = hold_sec > 0 and (now - p.entry_time).total_seconds() < hold_sec
        if hold_sec > 0 or time_confirm > 0:
            # a collapse to STOP_HOLD_EMERGENCY_MULT x the stop distance exits at once, in the hold and after it
            emergency = round(p.entry_credit + strategy.exit_setting(p.strategy, "STOP_HOLD_EMERGENCY_MULT", 3.0)
                              * level("STAGE0_STOP_PCT"), 2)
            if spread_price >= emergency:
                return self._close(p, p.remaining, "STOP_LOSS", now, spread_price)
        if in_hold:
            if spread_price >= stop_price:
                log.info("[%s] STAGE0 STOP ignored during the %ds entry hold at %.2f (stop %.2f, emergency %.2f)",
                         p.key, hold_sec, spread_price, stop_price, emergency)
            p.stop_streak = 0
            p.stop_first_at = None
        elif spread_price >= stop_price:
            if time_confirm > 0:
                if p.stop_first_at is None:
                    p.stop_first_at = now
                waited = (now - p.stop_first_at).total_seconds()
                if waited >= time_confirm:
                    return self._close(p, p.remaining, "STOP_LOSS", now, spread_price)
                log.info("[%s] STAGE0 STOP candidate for %.0fs/%ds at %.2f (stop %.2f): holding for confirmation",
                         p.key, waited, time_confirm, spread_price, stop_price)
                return []
            p.stop_streak += 1
            if p.stop_streak >= confirm:
                return self._close(p, p.remaining, "STOP_LOSS", now, spread_price)
            log.info("[%s] STAGE0 STOP candidate tick %d/%d (%ds) at %.2f (stop %.2f): holding for confirmation",
                     p.key, p.stop_streak, confirm, confirm_seconds, spread_price, stop_price)
            return []
        else:
            p.stop_streak = 0
            p.stop_first_at = None

        profit = round(p.entry_credit - spread_price, 2)
        if profit >= level("STAGE1_ARM_PCT"):
            p.stage1_armed = True
        if profit >= level("STAGE2_TARGET_PCT"):
            if not p.stage2_armed:
                # Anchor the stage-3 trail at the price that just crossed the target, NOT any deeper
                # (unconfirmed) best_price seen earlier -- that price hasn't held yet, so it shouldn't
                # be trusted as the trail's starting point either.
                p.confirmed_best_price = spread_price
            p.stage2_armed = True

        if p.stage2_armed:
            floor_price = round(p.entry_credit - level("STAGE2_FLOOR_PCT"), 2)
            if spread_price >= floor_price:
                return self._close(p, p.remaining, "STAGE2_FLOOR", now, spread_price)
            confirm_sec = config.STAGE3_CONFIRM_SECONDS
            held_long_enough = (p.best_price_since is not None
                                and (now - p.best_price_since).total_seconds() >= confirm_sec)
            if p.best_price < p.confirmed_best_price and held_long_enough \
                    and strategy.momentum_continuing(candles, p.direction):
                p.confirmed_best_price = p.best_price
                log.info("[%s] STAGE3 trail ratcheted to %.2f (held %ds)",
                         p.key, p.confirmed_best_price, confirm_sec)
            trail = level("STAGE3_TRAIL_PCT")
            if spread_price >= round(p.confirmed_best_price + trail, 2):
                return self._close(p, p.remaining, "STAGE3_TRAIL", now, spread_price)
            return []

        if p.stage1_armed:
            trail1 = level("STAGE1_TRAIL_PCT")
            if spread_price >= round(p.best_price + trail1, 2):
                return self._close(p, p.remaining, "STAGE1_TRAIL", now, spread_price)
            rsi_period = strategy.exit_setting(p.strategy, "RSI_PERIOD", 14)
            rsi_pts = strategy.exit_setting(p.strategy, "RSI_REVERSAL_PTS", 5.0)
            bb_period = strategy.exit_setting(p.strategy, "EXIT_BB_PERIOD", 20)
            bb_std = strategy.exit_setting(p.strategy, "EXIT_BB_STD", 2.0)
            band_touch = strategy.exit_setting(p.strategy, "EXIT_BB_TOUCH_PCT_B", 0.95)
            reversing = strategy.momentum_reversing(candles, p.direction, rsi_period, rsi_pts) \
                or strategy.bb_edge_reversal_risk(candles, p.direction, bb_period, bb_std, band_touch)
            if reversing:
                return self._close(p, p.remaining, "STAGE1_MOMENTUM_REVERSAL", now, spread_price)
        return []

    def _runner_trail(self, p: OpenSpread) -> float:
        """Trail width in points. Tightens to RUNNER_DEEP_TRAIL once the runner's best profit has
        passed RUNNER_DEEP_PROFIT, so a deep runner's pullback is caught sooner (per-strategy)."""
        if strategy.exit_setting(p.strategy, "RUNNER_TIGHTEN_ENABLED", False):
            deep_profit = strategy.exit_setting(p.strategy, "RUNNER_DEEP_PROFIT", 2.00)
            if round(p.entry_credit - p.runner_best, 2) >= deep_profit:
                return strategy.exit_setting(p.strategy, "RUNNER_DEEP_TRAIL", 0.30)
        return self._knob(p, "TRAIL_AMOUNT", config.TRAIL_AMOUNT)

    def _runner_tick(self, p: OpenSpread, now: datetime, spread_price: float, candles: pd.DataFrame | None) -> list[dict]:
        p.runner_best = min(p.runner_best, spread_price)
        reason = None
        if spread_price >= p.target_price:
            reason = "RUNNER_STOP"
        elif spread_price >= round(p.runner_best + self._runner_trail(p), 2):
            reason = "RUNNER_TRAIL"
        elif strategy.exit_setting(p.strategy, "RUNNER_SLOWDOWN_EXIT", True) \
                and p.momentum_at_target is not None and p.momentum_at_target > 0 \
                and strategy.momentum_slowed(strategy.momentum(candles, p.direction), p.momentum_at_target):
            reason = "RUNNER_MOMENTUM_SLOWED"
        if reason is None:
            return []
        return self._close(p, p.remaining, reason, now, spread_price)

    def force_close(self, now: datetime, prices: dict[str, float | None],
                    reason: str = "FORCE_CLOSE", override_hold: bool = False) -> list[dict]:
        """Close everything. `override_hold` (the hard stop) closes even while a MANUAL_HOLD file is present."""
        fills: list[dict] = []
        for key, p in list(self.positions.items()):
            price = prices.get(key)
            try:
                fills += self._close(p, p.remaining, reason, now, price if price is not None else p.current_price,
                                     override_hold=override_hold, retry_latched=True)
            except BrokerError as e:   # one position's broker error must not stop the others from closing
                log.critical("[%s] force close hit a broker error: %s -- continuing with the other positions", key, e,
                             extra={"keep": True})
                self.journal.event("CLOSE_FAILED", now, strategy=p.strategy, error=str(e), reason=reason)
        return fills

    # ----------------------------------------------- manual-override safety (2026-10-02)

    def mark_age(self, p: OpenSpread, now: datetime) -> float:
        """Seconds since this position last had a price (its entry counts as the first one)."""
        ref = p.last_mark_at or p.last_tick_at or p.entry_time
        return max((now - ref).total_seconds(), 0.0)

    def close_one(self, key: str, now: datetime, reason: str, override_hold: bool = False) -> list[dict]:
        """Close a single position at its last known price (used by the stale-data watchdog)."""
        p = self.positions.get(key)
        if p is None:
            return []
        return self._close(p, p.remaining, reason, now, p.current_price, override_hold=override_hold,
                           retry_latched=True)

    def any_close_blocked(self) -> bool:
        """True while any open position has a close that failed on every attempt (new entries are halted meanwhile)."""
        return any(p.close_fail_count > 0 for p in self.positions.values())

    def open_total_count(self) -> int:
        """Every open position, stock (F) or SPX: what MAX_OPEN_POSITIONS_TOTAL is checked against."""
        return len(self.positions)

    def open_spx_count(self) -> int:
        """Open non-stock positions (A-E count a trade only when it closes, so the daily total cap needs these)."""
        return sum(1 for p in self.positions.values() if p.strategy not in config.STOCK_STRATEGIES)

    def manual_hold(self) -> bool:
        """True while the owner has asked the bot to stand down from closing (see config.MANUAL_HOLD_FILE)."""
        folder = HERE / config.LOG_DIR
        return (folder / config.MANUAL_HOLD_FILE).exists() or             (folder / f"{config.MANUAL_HOLD_FILE}_{config.TRADER_NAME}").exists()

    def _sync_active(self) -> bool:
        """Dry-run brokers never hold what the bot "opens", so reconciling against them would read
        every position as closed outside the bot."""
        return bool(config.EXTERNAL_SYNC_ENABLED) and not getattr(self.broker, "dry_run", False)

    def _held_qty(self, p: OpenSpread, legs: list[dict]) -> tuple[int, int]:
        """(short contracts, long contracts) the broker really holds on this spread's two strikes."""
        short = long = 0
        for leg in option_legs(legs, {p.root}):
            if leg["right"] != p.right or leg["expiration"] != p.expiration:
                continue
            if leg["strike"] == p.short_strike and leg["qty"] < 0:
                short = abs(int(leg["qty"]))
            elif leg["strike"] == p.long_strike and leg["qty"] > 0:
                long = int(leg["qty"])
        return short, long

    def _flag_unbalanced(self, key: str, p: OpenSpread, short: int, long: int, now: datetime) -> None:
        """The two legs at the broker do not match each other (a hand-closed leg, or another spread netting
        one of our strikes away). Informational: raise a loud alert once. The close path decides what, if
        anything, can still be closed -- never more than the matched pairs, and nothing when only one leg is held."""
        if p.manual_attention:
            return
        p.manual_attention = True
        log.critical("[%s] legs at the broker do not match this spread (tracked %d, broker short %d / long %d) -- a leg "
                     "was closed by hand or netted away by another spread; automated closing can only act on matched "
                     "pairs, fix the rest by hand", key, p.remaining, short, long)
        self.journal.event("NAKED_LEG", now, strategy=p.strategy, key=key, tracked=p.remaining,
                           short_held=short, long_held=long)

    def sync_external(self, now: datetime) -> list[dict]:
        """Compare every tracked spread with what the broker actually holds (throttled to
        EXTERNAL_SYNC_SEC) and reconcile what was changed by hand in the broker UI:
          both legs gone      -> closed outside the bot: book it (last mark) and stop tracking it
          fewer contracts     -> a partial hand-close: book the difference, keep managing the rest
          a leg without its partner -> alert; the bot closes only matched pairs and never a spread whose leg is missing
          more contracts      -> the owner added some on the same strikes: the bot still only manages its own
        A position must look wrong on EXTERNAL_SYNC_CONFIRM consecutive syncs, and be older than
        EXTERNAL_MIN_AGE_SEC, before anything is changed. A failed read changes nothing."""
        if not self.positions or not self._sync_active():
            return []
        if self._last_sync is not None and (now - self._last_sync).total_seconds() < config.EXTERNAL_SYNC_SEC:
            return []
        self._last_sync = now
        try:
            legs = self.broker.get_positions()
        except BrokerError as e:
            log.error("EXTERNAL_SYNC could not read positions (nothing changed): %s", e)
            return []
        rows: list[dict] = []
        for key, p in list(self.positions.items()):
            if (now - p.entry_time).total_seconds() < config.EXTERNAL_MIN_AGE_SEC:
                continue
            short, long = self._held_qty(p, legs)
            if short == long and short >= p.remaining:
                if p.manual_attention:
                    p.manual_attention = False
                    log.warning("[%s] legs at the broker match this spread again -- automated closing resumed", key)
                if short > p.remaining and p.sync_obs != f"{short}/{long}":
                    log.warning("[%s] broker holds %d contracts on these strikes, bot tracks %d: managing only its own",
                                key, short, p.remaining)
                    p.sync_obs = f"{short}/{long}"
                elif short == p.remaining:
                    p.sync_obs = None
                p.sync_obs_count = 0
                continue
            obs = f"{short}/{long}"
            p.sync_obs_count = p.sync_obs_count + 1 if p.sync_obs == obs else 1
            p.sync_obs = obs
            if p.sync_obs_count < config.EXTERNAL_SYNC_CONFIRM:
                continue
            if short == 0 and long == 0:
                rows += self._book_external(p, p.remaining, now, "EXTERNAL_CLOSE")
            elif short == long:    # fewer than tracked, but a matched pair: a partial hand-close
                rows += self._book_external(p, p.remaining - short, now, "EXTERNAL_PARTIAL")
            else:                  # one leg missing or unequal sizes: a naked leg
                self._flag_unbalanced(key, p, short, long, now)
        return rows

    def _book_external(self, p: OpenSpread, qty: int, now: datetime, reason: str) -> list[dict]:
        """Book contracts that left the broker without this bot closing them (closed by hand). Their
        fill price is unknown, so the last accepted mark stands in; the row says external=True."""
        key = self.key_of(p)
        qty = min(qty, p.remaining)
        if qty <= 0:
            return []
        price = round(p.current_price, 2)
        pnl = round((p.entry_credit - price) * 100 * qty, 2)
        p.closed_qty += qty
        p.realized_pnl = round(p.realized_pnl + pnl, 2)
        p.sync_obs, p.sync_obs_count = None, 0
        row = self._make_row(p, key, now, qty, reason, exit_price=price, pnl=pnl, pnl_actual_fill=pnl,
                             trigger_price=price, exit_slippage=0.0, realistic_exit_price=price,
                             prev_price=None, prev_tick_pnl=None, prev_tick_jump=None)
        row["external"] = True
        log.warning("[%s] %s: %d contract(s) no longer at the broker (closed by hand?) -- booked at the last mark "
                    "%.2f, est. pnl %.2f | position realized %.2f, %d left", key, reason, qty, price, pnl,
                    p.realized_pnl, p.remaining)
        self.journal.event("EXIT", now, **row)
        if p.remaining == 0:
            del self.positions[key]
        return [row]

    def _read_held(self, p: OpenSpread) -> tuple[int, int] | None:
        try:
            legs = self.broker.get_positions()
        except BrokerError as e:
            log.error("[%s] could not read positions (%s)", self.key_of(p), e)
            return None
        return self._held_qty(p, legs)

    def _preflight(self, p: OpenSpread, now: datetime) -> tuple[list[dict], int, bool]:
        """Last look at the broker before sending a close. Returns (rows booked for what was already closed
        outside the bot, contracts the owner holds on the same strikes beyond ours, stop). A close is never
        sent for legs that are not held. If anything looks wrong it looks a second time before acting: one
        read can be empty or stale (Schwab answers [] without an error when its payload has no positions
        key), and wrongly dropping a live position would drop its stop -- when the two reads disagree the
        bot acts on neither and sends the close as before (a broker rejects a close for legs it does not hold)."""
        if not self._sync_active():
            return [], 0, False
        held = self._read_held(p)
        if held is None:
            return [], 0, False
        short, long = held
        if short == long and short >= p.remaining:
            return [], short - p.remaining, False
        if config.EXTERNAL_PREFLIGHT_RECHECK_SEC > 0:
            time.sleep(config.EXTERNAL_PREFLIGHT_RECHECK_SEC)
        if self._read_held(p) != held:
            return [], 0, False
        key = self.key_of(p)
        if short == 0 and long == 0:
            if (now - p.entry_time).total_seconds() >= config.EXTERNAL_MIN_AGE_SEC:
                return self._book_external(p, p.remaining, now, "EXTERNAL_CLOSE"), 0, True
            return [], 0, False
        if short != long:
            self._flag_unbalanced(key, p, short, long, now)
        pairs = min(short, long)
        if pairs == 0:
            return [], 0, True      # one leg only: there is no spread to close, send nothing
        rows = self._book_external(p, p.remaining - pairs, now, "EXTERNAL_PARTIAL") if pairs < p.remaining else []
        # whatever the broker holds beyond the pairs we are about to close (an odd leg) is not ours to close either
        return rows, max(short, long) - min(pairs, p.remaining), False

    def _remaining_at_broker(self, p: OpenSpread) -> int:
        legs = [leg for leg in option_legs(self.broker.get_positions(), {p.root})
                if leg["right"] == p.right and leg["expiration"] == p.expiration
                and leg["strike"] in (p.short_strike, p.long_strike)]
        return max((abs(leg["qty"]) for leg in legs), default=0)

    def _close(self, p: OpenSpread, qty: int, reason: str, now: datetime, spread_price: float,
               override_hold: bool = False, retry_latched: bool = False) -> list[dict]:
        if qty <= 0:
            return []
        key = self.key_of(p)
        if not retry_latched and p.close_blocked_until is not None and now < p.close_blocked_until:
            # a close failed on every attempt a moment ago: wait out the backoff instead of hammering the broker each tick
            if self._latch_logged.get(key) is None or (now - self._latch_logged[key]).total_seconds() >= 60:
                self._latch_logged[key] = now
                log.warning("[%s] close (%s) held back until %s after %d failed pass(es)", key, reason,
                            p.close_blocked_until.strftime("%H:%M:%S"), p.close_fail_count, extra={"keep": True})
            return []
        if self.manual_hold() and not override_hold:
            if self._hold_logged is None or (now - self._hold_logged).total_seconds() >= 60:
                self._hold_logged = now
                log.warning("[%s] MANUAL_HOLD file present: NOT closing (%s) -- delete logs/%s to resume",
                            key, reason, config.MANUAL_HOLD_FILE, extra={"keep": True})
            return []
        pre_rows: list[dict] = []
        order = None
        left = p.remaining - qty
        expected_left = left
        owner_extra = 0
        for attempt in range(1, config.CLOSE_MAX_RETRIES + 1):
            # the preflight runs before EVERY attempt, not just the first: close_spread_at_market can work a
            # limit order for a long time and the owner may hand-close the position while it does
            rows, owner_extra, stop = self._preflight(p, now)
            pre_rows += rows
            if stop:
                return pre_rows
            qty = min(qty, p.remaining)
            if qty <= 0:
                return pre_rows
            expected_left = p.remaining - qty
            if attempt == 1:
                log.warning("[%s] CLOSING %d/%d %s (%s) at market, spread mid %.2f",
                            key, qty, p.remaining, p.right, reason, spread_price)
            try:
                order = self.broker.close_spread_at_market(
                    p.underlying, p.expiration, p.right, p.short_strike, p.long_strike, qty,
                    time_in_force="day", root=p.root)
            except BrokerError as e:
                order = None
                log.error("[%s] CLOSE_FAILED attempt %d/%d: %s", key, attempt, config.CLOSE_MAX_RETRIES, e)
                self.journal.event("CLOSE_FAILED", now, strategy=p.strategy, attempt=attempt,
                                   error=str(e), reason=reason)
                continue
            if order.get("status") == "dry_run":
                break
            try:
                left = max(self._remaining_at_broker(p) - owner_extra, 0)
            except BrokerError as e:
                # 2026-10-07: the positions read AFTER the order failed (an Alpaca 429, say). Raising here dropped the rows
                # already built for the other positions of a force-close pass. A fill the broker itself reports as filled
                # is trusted; anything else is retried (the next attempt's preflight re-reads and only sends what is held).
                log.error("[%s] close verification read failed (%s); order status %s", key, e, order.get("status"))
                self.journal.event("CLOSE_VERIFY_FAILED", now, strategy=p.strategy, attempt=attempt, error=str(e),
                                   order_status=order.get("status"), reason=reason)
                if order.get("status") == "filled":
                    left = expected_left
                    break
                order = None
                continue
            if left <= expected_left:
                break
            log.error("[%s] CLOSE_FAILED attempt %d/%d: broker still shows %d contracts (expected %d)",
                      key, attempt, config.CLOSE_MAX_RETRIES, left, expected_left)
            self.journal.event("CLOSE_FAILED", now, strategy=p.strategy, attempt=attempt, broker_qty=left,
                               expected=expected_left, reason=reason)
            order = None
        else:
            log.critical("[%s] CLOSE_FAILED: %s legs still open after %d attempts -- MANUAL ACTION REQUIRED",
                         key, p.root, config.CLOSE_MAX_RETRIES)
            p.close_fail_count += 1
            backoff = tuple(config.CLOSE_BLOCK_BACKOFF_SEC) or (60,)
            delay = backoff[min(p.close_fail_count, len(backoff)) - 1]
            p.close_blocked_until = now + timedelta(seconds=delay)
            self.journal.event("CLOSE_BLOCKED", now, strategy=p.strategy, key=key, fail_count=p.close_fail_count,
                               retry_at=p.close_blocked_until, backoff_sec=delay, reason=reason)
            log.critical("[%s] close latched for %ds (pass %d): new entries are halted until it closes", key, delay,
                         p.close_fail_count, extra={"keep": True})
            return pre_rows
        p.close_fail_count = 0           # the close went through (fully or in part): clear the latch
        p.close_blocked_until = None
        exit_price = order.get("filled_avg_price") if order else None
        exit_price = float(exit_price) if exit_price is not None else spread_price
        fill_qty = p.remaining - left
        # 2026-09-30: diagnostic instrumentation for a live anomaly -- ASTRA's reported price jumped
        # from 5.21 to ~6.10 in the ONE TICK between reporting and exiting, too large a move for that
        # second's real underlying action. prev_price is current_price as of the PRIOR tick (captured
        # in _tick_position before this tick's spread_price overwrote it) -- prev_tick_pnl is what
        # today's real pnl would compare against had the decision used that prior quote instead,
        # and prev_tick_jump isolates the tick-to-tick DECISION price move itself (spread_price vs the
        # prior tick), separate from exit_slippage (which is fill vs. THIS tick's decision). None on
        # the very first tick after entry (no prior tick yet) or on a force-close (no tick preceded it).
        prev_price = p.prev_price
        max_age = getattr(config, "PREV_TICK_MAX_AGE_SEC", 0)
        if prev_price is not None and max_age and p.prev_price_at is not None \
                and (now - p.prev_price_at).total_seconds() > max_age:
            prev_price = None   # 2026-10-08: that tick predates an outage: price the exit at the trigger, not at the pre-outage mark
        prev_tick_pnl = round((p.entry_credit - prev_price) * 100 * fill_qty, 2) if prev_price is not None else None
        prev_tick_jump = round(spread_price - prev_price, 2) if prev_price is not None else None
        # 2026-10-01, revised same day: the bot's OWN official P/L (pnl, realized_pnl -- everything
        # downstream: RiskManager.record_trade, daily-loss-limit tracking, consecutive-loss counting,
        # the cards/CSV) runs on realistic_exit_price = the tick immediately BEFORE the one that
        # triggered this exit (prev_price), not a flat slippage assumption -- user's explicit call: the
        # paper sim's actual fills swing wildly and adversely during fast moves, and the tick right
        # before the trigger is the closest stand-in for "the print before close" a live fill would
        # realistically land near. Falls back to spread_price itself when there's no prior tick (first
        # tick after entry, or a force-close). The REAL broker fill is still fully recorded (exit_price,
        # pnl_actual_fill) for comparison -- it's just no longer what the bot's own risk management
        # reacts to. This is a genuine, deliberate divergence from the broker's own account equity,
        # which still reflects the real fill; that gap is the whole point, not a bug.
        realistic_exit_price = round(prev_price, 2) if prev_price is not None else round(spread_price, 2)
        pnl = round((p.entry_credit - realistic_exit_price) * 100 * fill_qty, 2)
        pnl_actual_fill = round((p.entry_credit - exit_price) * 100 * fill_qty, 2)
        p.closed_qty += fill_qty
        p.realized_pnl = round(p.realized_pnl + pnl, 2)
        # Fill quality: positive slippage = the fill cost us relative to the quote mid that triggered it.
        # (Still measured against the REAL fill -- this is a pure execution-quality diagnostic, separate
        # from which P/L the bot treats as official.)
        exit_slippage = round(exit_price - spread_price, 2)
        entry_slippage = round(p.entry_mid - p.entry_credit, 2) if p.entry_mid is not None else None
        row = self._make_row(p, key, now, fill_qty, reason, exit_price=exit_price, pnl=pnl,
                             pnl_actual_fill=pnl_actual_fill, trigger_price=spread_price,
                             exit_slippage=exit_slippage, realistic_exit_price=realistic_exit_price,
                             prev_price=prev_price, prev_tick_pnl=prev_tick_pnl, prev_tick_jump=prev_tick_jump,
                             entry_slippage=entry_slippage)
        log.warning("[%s] CLOSED %d @ %.2f (%s, trigger %.2f, slippage %+.2f) pnl %.2f (OFFICIAL, realistic "
                    "@%.2f) | actual broker fill would've booked %.2f | prev-tick %.2f (jump %+.2f, pnl "
                    "would've been %.2f) | position realized %.2f, %d left",
                    key, fill_qty, exit_price, reason, spread_price, exit_slippage, pnl, realistic_exit_price,
                    pnl_actual_fill,
                    prev_price if prev_price is not None else float("nan"),
                    prev_tick_jump if prev_tick_jump is not None else float("nan"),
                    prev_tick_pnl if prev_tick_pnl is not None else float("nan"),
                    p.realized_pnl, p.remaining)
        self.journal.event("EXIT", now, **row)
        if p.remaining == 0:
            del self.positions[key]
        return pre_rows + [row]

    def _make_row(self, p: OpenSpread, key: str, now: datetime, fill_qty: int, reason: str, *, exit_price: float,
                  pnl: float, pnl_actual_fill: float, trigger_price: float, exit_slippage: float,
                  realistic_exit_price: float, prev_price: float | None, prev_tick_pnl: float | None,
                  prev_tick_jump: float | None, entry_slippage: float | None = None) -> dict:
        """The EXIT row every close path (the bot's own and externally detected ones) hands to the journal,
        the CSV, the cards and the risk manager -- call after p.closed_qty / p.realized_pnl are updated."""
        scan = p.scan or {}
        if entry_slippage is None and p.entry_mid is not None:
            entry_slippage = round(p.entry_mid - p.entry_credit, 2)
        return {
            "date": now.date().isoformat(), "strategy": p.strategy,
            "entry_time": p.entry_time, "exit_time": now,
            "direction": p.direction, "setup": p.setup, "right": p.right,
            "short_strike": p.short_strike, "long_strike": p.long_strike, "width": p.width,
            "qty": fill_qty, "entry_credit": p.entry_credit, "exit_price": exit_price,
            "pnl": pnl, "exit_reason": reason, "runner": "y" if p.runner else "n",
            "trigger_price": trigger_price, "exit_slippage": exit_slippage,
            "realistic_exit_price": realistic_exit_price, "pnl_actual_fill": pnl_actual_fill,
            "prev_tick_price": prev_price, "prev_tick_pnl": prev_tick_pnl, "prev_tick_jump": prev_tick_jump,
            "entry_mid": p.entry_mid, "entry_slippage": entry_slippage,
            "position_closed": p.remaining == 0, "position_pnl": p.realized_pnl,
            "total_qty": p.qty, "root": p.root, "key": key, "ticker": p.ticker,
            "expiration": p.expiration.isoformat(), "mfe": p.mfe, "mae": p.mae,
            "scanner_score": scan.get("score"), "scanner_direction": scan.get("scanner_direction"),
            "direction_match": scan.get("direction_match"),
        }
