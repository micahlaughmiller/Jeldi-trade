"""Flatten NOW: close every position this persona holds, at the market, and stop.

The emergency hand-brake (19 Nov 2026: the broker UI would not let a position be closed by hand). It runs the same code as
the hard stop (Bot.end_of_day): cancel this bot's open orders, adopt the spreads at the broker, close each one marketable
(reason FORCE_CLOSE is urgent: the ask side + CLOSE_SLIPPAGE from the first order, climbing a tick a retry), retry until
flat, then write the day's report and state. A MANUAL_HOLD file does not stop it.

    python flatten_now.py               close everything this bot opened today (tracked now, or in today's saved state)
    python flatten_now.py --adopt-all   also every other SPXW leg expiring today in the account (not the 45DTE bot's spreads)
    python flatten_now.py --dry-run     log the orders instead of sending them

If the bot is running, press Ctrl-C ONCE in its window instead (CLOSE_ON_INTERRUPT closes everything the same way). This
script refuses to start while the bot holds the instance lock, so the two never work the same spread.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date

import config
import instance_lock
import scheduler
from broker import BrokerError
from risk_manager import DayState, RiskManager
from scheduler import LOG_DIR, log, now_et


def flatten(bot: scheduler.Bot) -> bool:
    """Adopt what the broker holds and run the hard-stop close until flat. True when flat."""
    now = now_et()
    acct = bot.broker.get_account()
    saved = bot.journal.load_state()
    saved_today = saved if saved and saved.get("date") == bot.today.isoformat() else None
    saved_positions = (saved_today or {}).get("positions") or {}
    bot._saved_spxw = {(d["right"], float(d[k]), date.fromisoformat(d["expiration"]))
                       for d in saved_positions.values()
                       if d and d.get("root", config.OPTION_ROOT) == config.OPTION_ROOT
                       for k in ("short_strike", "long_strike")}
    bot._saved_stock = {(d["root"], d["right"], date.fromisoformat(d["expiration"]), float(d[k]))
                        for d in saved_positions.values()
                        if d and d.get("root", config.OPTION_ROOT) != config.OPTION_ROOT
                        for k in ("short_strike", "long_strike")}
    if saved_today:
        bot.risk = RiskManager(acct["equity"], DayState.from_dict(saved_today["day"]))
        bot.acted = set(saved_today.get("acted", []))
    else:
        bot.risk = RiskManager(acct["equity"])
    cap = acct.get("capital_cap")
    if cap:
        bot.risk.state.start_equity = cap
    positions = bot.broker.get_positions()
    legs = bot.bot_legs(positions)
    foreign = bot.foreign_legs(positions)
    if foreign:
        log.warning("Leaving %d option leg(s) that are not this persona's: %s%s", len(foreign),
                    [(l["symbol"], l["qty"]) for l in foreign],
                    "" if config.ADOPT_UNTRACKED_SPXW else "  (--adopt-all closes same-day SPXW legs too)", extra={"keep": True})
    if legs:
        bot.pm.adopt_from_broker(legs, now, bot.broker.get_spot(config.UNDERLYING), saved_positions or None)
    if not bot.pm.positions:
        log.warning("Nothing to close: no %s option legs at the broker.", config.TRADER_NAME, extra={"keep": True})
        return True
    log.critical("FLATTEN NOW: closing %d position(s) at the market: %s", len(bot.pm.positions),
                 ", ".join(f"{k} {p.direction} {p.short_strike:g}/{p.long_strike:g}{p.right} x{p.remaining}"
                           for k, p in bot.pm.positions.items()), extra={"keep": True})
    while True:
        done = bot.end_of_day(now_et())
        if done:
            return True
        now = now_et()
        if now.time() >= config.MARKET_CLOSE:
            return False
        wait = int(getattr(config, "EOD_CLOSE_RETRY_SEC", 60))
        log.critical("Still not flat: trying again in %ds (Ctrl-C stops this script; the legs stay at the broker).", wait,
                     extra={"keep": True})
        time.sleep(wait)


def main() -> None:
    parser = argparse.ArgumentParser(description="Close every position this 0DTE persona holds, at the market, now.")
    parser.add_argument("--dry-run", action="store_true", help="log orders instead of sending them")
    parser.add_argument("--adopt-all", action="store_true",
                        help="also close same-day SPXW legs the bot did not open (sets ADOPT_UNTRACKED_SPXW for this run)")
    args = parser.parse_args()
    scheduler.setup_logging()
    scheduler.install_signal_handlers()
    if args.adopt_all:
        config.ADOPT_UNTRACKED_SPXW = True
    if getattr(config, "SINGLE_INSTANCE_LOCK", True):
        ok, holder = instance_lock.acquire(LOG_DIR, f"0dte_{config.TRADER_NAME.lower()}")
        if not ok:
            log.critical("%s is running (%s). Press Ctrl-C ONCE in its window: it closes everything itself. "
                         "Or close that window, then run this again.", config.TRADER_NAME, holder, extra={"keep": True})
            sys.exit(2)
    bot = scheduler.Bot(dry_run=args.dry_run or config.DRY_RUN, once=True)
    try:
        flat = flatten(bot)
    except KeyboardInterrupt:
        log.warning("Interrupted. Check the account: whatever is still open is NOT managed by anything.", extra={"keep": True})
        sys.exit(130)
    except BrokerError as exc:
        log.error("BROKER ERROR: %s", exc)
        if getattr(exc, "reauth_command", None):
            log.error("The Schwab token is missing, expired or rejected. Re-login with:  %s", exc.reauth_command)
        sys.exit(1)
    if flat:
        log.warning("FLAT. Everything this persona held is closed.", extra={"keep": True})
    else:
        log.critical("NOT FLAT at the session end. MANUAL ACTION REQUIRED at the broker.", extra={"keep": True})
        sys.exit(1)


if __name__ == "__main__":
    main()
