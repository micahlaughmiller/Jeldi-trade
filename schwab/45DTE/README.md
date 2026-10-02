# 45-60 DTE S&P 500 Credit-Spread Bot (Schwab)

Scans the S&P 500 for daily RSI(14)/RSI(28) confluence, sells $5-wide, ~0.30-delta
vertical credit spreads 45-60 days out, and works each position to a 50 % profit target
with a resting GTC close. All broker access goes through `broker.py`
(`docs/BROKER_INTERFACE.md`); nothing else imports the Schwab SDK.

## Setup

```
python -m venv venv && venv\Scripts\activate
pip install -r requirements.txt
```

Create `schwab/45DTE/.env` (never commit it; `.gitignore` already excludes it):

```
SCHWAB_APP_KEY=...
SCHWAB_APP_SECRET=...
SCHWAB_CALLBACK_URL=https://127.0.0.1:8080
SCHWAB_ACCOUNT_INDEX=0
SCHWAB_MODE=sim            # sim | dry_run | live (see "Paper simulation")
SIM_STARTING_EQUITY=30000  # simulated account size in sim mode
SCHWAB_LIVE_ORDERS=false   # live mode only: true -> REAL orders are submitted
```

Then run `python schwab_login.py` once to create `token.json` (refresh token lasts 7 days).

`config_45dte.py` reads these with `os.getenv`; it contains no literal keys.

## Run

```
cd schwab/45DTE
python run_45dte.py                  # trade per SCHWAB_MODE (sim by default)
python run_45dte.py --dry-run        # read from broker, log orders instead of sending them
python run_45dte.py --once           # exit after today's 16:05 end-of-day summary
python run_45dte.py --reset-breaker  # clear the max-loss circuit breaker, then run
python sim_reset.py [--equity N]     # wipe the simulated account (sim mode)
```

## Paper simulation

Schwab has no paper-trading API, so `SCHWAB_MODE` selects what `broker.Broker` returns:

| Mode | Broker | Orders |
| --- | --- | --- |
| `sim` (default) | `paper_sim.PaperBroker` wrapping a dry-run `SchwabBroker` for market data | Filled locally against live Schwab quotes; account persisted in `logs/sim_state.json` |
| `dry_run` | `SchwabBroker(dry_run=True)` | Payloads logged, nothing sent |
| `live` | `SchwabBroker` | Real orders, only with `SCHWAB_LIVE_ORDERS=true`; otherwise forced to dry-run |

`--dry-run` always gives the log-only broker regardless of mode. The simulator starts
with `SIM_STARTING_EQUITY` cash and fills all-or-nothing at the limit during regular
hours (09:30-16:00 ET; SPX/SPXW until 16:15): a credit spread when short bid - long ask
reaches the limit, a debit close when short ask - long bid falls to it. `day` orders
expire at the close, `gtc` orders persist across restarts, legs past expiration
cash-settle at intrinsic value. Equity = cash + marks; options buying power = cash -
(width - credit) x 100 x qty per spread. Every event is logged with a `SIM` prefix
(`SIM ORDER new`, `SIM FILL`, `SIM CANCEL`, `SIM REPLACE`, `SIM EXPIRE`, `SIM SETTLE`),
and the daily equity journal goes to `logs/sim_equity_history.csv`. The start-of-day
report shows `broker=schwab-sim PAPER`.

`run_45dte.py` `chdir`s to its own folder, so `logs/` and `data/` resolve regardless of
where you launch from. Ctrl+C saves state and exits.

## What happens during a day (all times US/Eastern)

1. **Launch: start-of-day report.** Broker name and PAPER/LIVE, equity, cash, options
   buying power, YTD/MTD/today P&L, every open order (symbol, class, side, qty, limit,
   TIF, status), and open positions reconstructed as spreads. `record_daily_equity()`
   appends today's equity to `logs/equity_history.csv`.
2. **Adopt.** Option legs at the broker are paired by underlying/expiration/right
   (short leg qty < 0, long leg qty > 0, long further OTM, equal size). Entry credit =
   short avg price - long avg price. Existing GTC close orders are matched so none are
   duplicated; untracked opening $5-wide spread orders on S&P 500 names are adopted as
   working entries. Orders on anything else (e.g. the 0DTE bot's SPXW spreads on the same
   account) are logged as `UNKNOWN_OPEN_ORDER` and never touched. Legs that do not pair
   log `UNPAIRED_LEG` and that underlying is never traded.
3. **Scan cadence** (`SCAN_SCHEDULE`): 09:30-11:30 every 5 min, 11:30-15:00 every 30 min,
   15:00-16:00 every 5 min. A late start runs the most recent missed slot immediately.
   Each scan downloads one year of daily bars for all 503 names (today's live bar is the
   last row), computes Wilder RSI(14) and RSI(28), and logs `SCAN_RESULT` with ok/failed
   counts and elapsed seconds (about 20 s with 8 download workers).
4. **Entry.** For each signal: choose the expiration in 45-60 DTE nearest 52 (or the
   nearest within 10 days of the window, flagged `dte_out_of_range`); short strike =
   |delta| closest to 0.30 inside [0.20, 0.40]; long strike $5 further OTM (must exist,
   else the next delta candidate, else skip). Credit = mid - mid rounded down to $0.05.
   Accept at >= 1.50 (>= 1.40 when both RSIs are beyond 25/75). Risk check, then one
   multi-leg DAY limit order at the credit. Every 60 min the limit drops $0.02 via
   replace, down to the floor (1.50 / 1.40 strong), where it sits working. All
   unfilled entries are canceled at 15:55; no entries are started after that.
5. **Fill -> GTC close.** On fill the actual `filled_avg_price` becomes the entry
   credit and a GTC close is submitted at `round_to_0.05(credit x 0.50)`. Partial fills
   get a close for the filled size; the close is replaced when the rest fills.
6. **Maintenance** every 60 s: detect fills, price reductions, sync with manual changes made in
   the broker platform (see Manual overrides), re-create any close order
   that went missing/rejected/expired (one you cancelled or edited yourself is respected, see
   Manual overrides), refresh spread price (short mid - long
   mid) for P&L, then apply the exit rules. **Schwab's only exits** are the GTC close at
   50 % of max profit and the profit-floor trail: once the spread is 40 % of the way to that
   target (20 % of max profit) the first 5-cent give-back from the best price exits at market
   -- if it still shows at least `PROFIT_FLOOR_MIN_PROFIT` of profit; if it reversed through
   entry first, the floor disarms and the position rides. There is **no DTE force-exit and no
   max-loss stop** (`EXIT_DTE_ENABLED`/`MAX_LOSS_EXIT` are off): an under-water spread rides to
   the target or to expiration, where the broker settles it (booked `expired`, P&L unknown, with
   the last mark kept as an estimate; shares left by assignment are flagged `EXPIRED_SHARES`).
   Each max-loss *mark* still counts toward the circuit breaker.
7. **Reports** every 60 min and after any fill/close: open positions (right, strikes,
   exp, DTE, qty, entry, current, unrealized, close-order state), working entries,
   closed-today with realized P&L, totals, portfolio risk % of equity, breaker state.
   A `DAILY_LOSS_ALERT` fires if equity is down 3 % from the day's start.
8. **16:05: reconcile + daily summary**, then idle until the next session (or exit with
   `--once`). Weekends and `MARKET_HOLIDAYS` are skipped.

### Circuit breaker

Three spreads hitting max loss (current price >= credit + 0.9 x (5 - credit), or a
realized loss >= 90 % of max loss) trip the breaker: no new entries until you restart
with `--reset-breaker`. Existing positions are still managed. On Schwab (no max-loss stop)
a hit is recorded when the spread's *mark* first reaches that level, even if it later
recovers. State persists in `logs/state.json`.

### Manual overrides (thinkorswim)

You can close or cancel things by hand in thinkorswim at any time. Every maintenance cycle
(60 s) the bot makes one `get_positions()` read (`sync_external`, the first thing
`maintain()` does) and follows what the broker really holds instead of fighting it. A
mismatch must show up **identically on `EXTERNAL_SYNC_CONFIRM_CYCLES` consecutive cycles**
before the bot acts (so expect it to notice 1-2 minutes after you act; while a mismatch is
unconfirmed it places nothing for that spread), spreads opened or last filled less than
`EXTERNAL_SYNC_MIN_AGE_SEC` ago are not checked (a positions feed that lags a fill can never
make it drop a real position), and a failed read is logged (`POLL_ERROR`) and skipped.
Skipped entirely when `EXTERNAL_SYNC_ENABLED` is False or the broker is in dry-run.

| What you do in thinkorswim | What the bot does |
| --- | --- |
| Close the whole spread (both legs gone) | Cancels its own GTC close (best effort), books the position as `closed outside the bot (manual)` with unknown exit (the last mark is kept as `last_price_before_exit` / `est_realized_pl`), logs `EXTERNAL_CLOSE`. **Not** counted toward the circuit breaker. Starts a `MANUAL_CLOSE_COOLDOWN_SEC` pause on NEW entries (nothing is blocked; the symbol is free and the next signal after the cool-down enters normally). If our own GTC target filled instead, it is booked as a normal profit-target fill. |
| Close part of it (both legs reduced equally) | Shrinks the tracked qty to what is held (`EXTERNAL_PARTIAL_CLOSE`) and re-places the GTC close at the new size. No cool-down. (If the bot's own GTC part-fills, that is recognised and left alone.) |
| Close one leg only, or leave unequal legs | Loud `NAKED_LEG` alert (re-alerted at most once a day) and the spread is marked manual-attention: **no order activity at all** (no GTC re-creation, no exits, no max-loss/trail logic, no cancels) until the legs are consistent again (`MANUAL_ATTENTION_CLEARED`) or both are gone (booked as a manual close). The bot's own resting GTC is left as it is -- check it in thinkorswim. |
| Hold extra contracts on the same strikes | `EXTERNAL_EXTRA_CONTRACTS` is logged and the extras are ignored: the bot only ever manages and closes its own qty, and its exit verification is a count check, so your contracts are never mistaken for a failed close. |
| Cancel the bot's GTC close | `CLOSE_ORDER_CANCELED_BY_OWNER`; the spread is put on manual hold and the GTC is **never re-created** (the profit-floor trail and other exits keep running). A genuinely new fill of the same entry clears the hold and places the close for the new size. |
| Edit the GTC close (thinkorswim replaces it: old order `replaced`, new order id) | The new open order for the same legs is adopted (`CLOSE_ORDER_ADOPTED`) and tracked to its fill. |
| (Bot-side) a close that was rejected, expired or went missing | Re-created as before. |

When the bot itself exits at market it first re-reads the positions: both legs already gone
-> nothing is sent and it is booked as a manual close; one leg -> frozen as above; otherwise
it closes `min(its qty, what is held)` and verifies by contract count. On restart, `adopt`
keeps a tracked spread whose legs sit next to other legs of yours (the underlying stays
blocked until you fix it), cancels the leftover close order of a position that vanished, and
never adopts an open order shaped like a closing order (put sold strike below bought strike /
call sold above bought) as an entry.

**Stopping the bot is still the safest way to take over** (Ctrl+C saves state; the next start
re-adopts whatever the broker holds). Known limits: extra contracts of yours on the bot's
strikes are absorbed into the tracked qty if the bot is restarted (`QTY_MISMATCH` "using
broker"); how thinkorswim reports a cancelled vs an edited order is mapped from Schwab's
`CANCELED` / `REPLACED` order statuses and has not been exercised against a live account.

## Files written to `logs/`

| File | Content |
| --- | --- |
| `session_YYYY-MM-DD.jsonl` | one JSON event per line (also echoed to console) |
| `trades_YYYY-MM-DD.csv` | `ENTRY_FILL` and `CLOSE` rows with credit, debit, realized P&L |
| `summary_YYYY-MM-DD.json` | EOD: counts (scans, signals, entries, fills, closes), equity start/end, realized/unrealized, open positions, closed today, breaker, reconcile |
| `reconcile_YYYY-MM-DD.json` | local state vs broker legs and orders; mismatches also logged as `RECONCILE_MISMATCH` |
| `state.json` | open spreads, working entries, closed history, breaker, day counters |
| `equity_history.csv` | daily equity journal (written by `broker.py`) |

## Tests

```
cd schwab/45DTE
python -m pytest tests -q
```

`tests/fake_broker.py` implements the broker interface in memory; the strategy tests never
touch the network. `tests/test_broker_schwab.py` covers the Schwab broker with a mocked client; no live Schwab order is ever sent by tests.

## Configuration (`config_45dte.py`)

| Knob | Default | Meaning |
| --- | --- | --- |
| `SCHWAB_MODE` | `sim` | `sim` (local simulator on live quotes), `dry_run` (log only), `live` (real orders, needs `SCHWAB_LIVE_ORDERS=true`) |
| `SIM_STARTING_EQUITY` | `30000` | simulated account cash on first run / after `sim_reset.py` |
| `SIM_QUOTE_CACHE_SEC` | `10` | simulator chain-quote cache |
| `SIM_FILL_START` / `SIM_FILL_END` / `SIM_INDEX_FILL_END` | `09:30` / `16:00` / `16:15` | simulator fill window (index options until 16:15) |
| `DRY_RUN` | true in `dry_run`/`live` modes unless `SCHWAB_LIVE_ORDERS=true`; always false in `sim` | log order payloads instead of sending (`--dry-run` forces on) |
| `RISK_FREE_RATE` | `0.04` | for Black-Scholes delta when the feed has no greeks |
| `CLOSE_SLIPPAGE` / `CLOSE_RETRY_SEC` / `CLOSE_MAX_RETRIES` | `0.05` / `15` / `6` | marketable-close ladder used by `close_spread_at_market` |
| `LOG_DIR` | `logs` | all log/state output |
| `UNIVERSE_FILE` | `data/sp500_tickers.txt` | one broker-spelled ticker per line (`BRK.B`; mapped to `BRK-B` for yfinance) |
| `HISTORY_PERIOD` | `1y` | daily bars downloaded per ticker |
| `DOWNLOAD_WORKERS` | `8` | processes, each running one batched `yf.download` over a slice of the universe (1 = single call, ~130 s) |
| `RSI_FAST` / `RSI_SLOW` | `14` / `28` | Wilder RSI periods on daily closes |
| `RSI_OVERSOLD` / `RSI_OVERBOUGHT` | `30` / `70` | both RSIs beyond -> put / call credit spread |
| `RSI_STRONG_OVERSOLD` / `RSI_STRONG_OVERBOUGHT` | `25` / `75` | both beyond -> `strong` signal (lower credit floor) |
| `DTE_MIN` / `DTE_MAX` / `DTE_TARGET` | `45` / `60` / `52` | expiration window and preferred DTE |
| `DTE_TOLERANCE_DAYS` | `10` | accept 35-70 DTE when nothing is inside the window (flagged) |
| `DTE_SEARCH_MIN` / `DTE_SEARCH_MAX` | `30` / `80` | range requested from `get_expirations` |
| `TARGET_DELTA` / `DELTA_MIN` / `DELTA_MAX` | `0.30` / `0.20` / `0.40` | short-strike selection |
| `SPREAD_WIDTH` | `5.0` | long strike distance; max loss = width - credit |
| `MIN_CREDIT` / `MIN_CREDIT_STRONG` | `1.50` / `1.40` | acceptance threshold and price-reduction floor |
| `POSITION_SIZE_TIERS` | `{10k:3, 30k:5, 50k:10, inf:15}` | max contracts by equity (equity < key) |
| `MAX_RISK_PER_TRADE_PCT` | `0.04` | contracts = floor(equity x 4 % / (max loss x 100)) |
| `MAX_PORTFOLIO_RISK_PCT` | `0.52` | cap on sum(qty x max loss x 100) incl. working entries; size is reduced to fit |
| `ALLOW_MIN_CONTRACT_OVERRIDE` | `True` | allow 1 contract when sizing gives 0 and the cap permits |
| `MAX_NEW_POSITIONS_PER_DAY` | `10` | entry orders per day |
| `MAX_LOSS_HITS_CIRCUIT_BREAKER` | `3` | hits that halt new entries |
| `MAX_LOSS_HIT_PCT` | `0.90` | fraction of max loss that counts as a hit / triggers exit |
| `DAILY_LOSS_ALERT_PCT` | `0.03` | equity drop from day start that logs `DAILY_LOSS_ALERT` |
| `PROFIT_TARGET_PCT` | `0.50` | GTC close debit = credit x (1 - this), rounded to $0.05 |
| `EXIT_DTE` | `7` | close at market at or below this DTE (only when `EXIT_DTE_ENABLED`) |
| `EXIT_DTE_ENABLED` | `False` (Schwab) | `False` = never force-close on DTE; positions ride to expiration |
| `MAX_LOSS_EXIT` | `False` (Schwab) | `True` = close at market on a max-loss hit; hits are counted toward the breaker either way |
| `PROFIT_FLOOR_ENABLED` / `_ARM_PCT_OF_TARGET` | `True` / `0.40` (Schwab) | floor arms 40% of the way to the target (20% of max profit) |
| `PROFIT_FLOOR_MODE` / `PROFIT_FLOOR_TRAIL_TICK` | `trail` / `0.05` (Schwab) | once armed, exit at market on the first 5-cent give-back from the best price if that still books a profit; if it reversed through entry first, disarm and ride |
| `EXTERNAL_SYNC_ENABLED` | `True` | each maintenance cycle compare tracked spreads with `get_positions()` and follow manual changes (see Manual overrides) |
| `EXTERNAL_SYNC_CONFIRM_CYCLES` | `2` | identical inconsistent observations required in a row before the bot acts on a manual change |
| `EXTERNAL_SYNC_MIN_AGE_SEC` | `120` | spreads opened / last filled more recently than this are not synced (positions-feed lag) |
| `MANUAL_CLOSE_COOLDOWN_SEC` | `120` | no NEW entry for this long after a spread is found fully closed by hand |
| `ENTRY_TIME_IN_FORCE` | `day` | entry orders; closes are always `gtc` |
| `PRICE_REDUCTION_INTERVAL_MIN` / `PRICE_REDUCTION_AMOUNT` | `60` / `0.02` | entry limit walk-down |
| `CANCEL_UNFILLED_AT` | `15:55` | cancel working entries; no new entries after this |
| `SCAN_SCHEDULE` | see above | `(start, end, minutes)` windows |
| `MAINTENANCE_INTERVAL_SEC` | `60` | fill detection / close upkeep / P&L refresh |
| `REPORT_INTERVAL_MIN` | `60` | position report cadence |
| `MARKET_OPEN` / `MARKET_CLOSE` / `EOD_TIME` | `09:30` / `16:00` / `16:05` | session bounds and EOD job |
| `LOOP_SLEEP_SEC` | `15` | main-loop sleep (kept short so slot boundaries are hit) |
| `IDLE_LOG_INTERVAL_SEC` | `300` | how often an `IDLE` line is logged outside the session |
| `MARKET_HOLIDAYS` | 2026 NYSE list | days skipped entirely |

## Notes

