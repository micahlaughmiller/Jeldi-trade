# 45-60 DTE S&P 500 Credit-Spread Bot (Alpaca)

Scans the S&P 500 for daily RSI(14)/RSI(28) confluence, sells $5-wide, ~0.30-delta
vertical credit spreads 45-60 days out, and works each position to a 50 % profit target
with a resting GTC close. All broker access goes through `broker.py`
(`docs/BROKER_INTERFACE.md`); nothing else imports `requests` or an Alpaca SDK.

## Setup

```
python -m venv venv && venv\Scripts\activate
pip install -r requirements.txt
```

Create `Alpaca/45DTE/.env` (never commit it; `.gitignore` already excludes it):

```
ALPACA_API_KEY=PK...
ALPACA_SECRET_KEY=...
ALPACA_PAPER=true                 # false -> live URL
# ALPACA_BASE_URL=...             # optional override
# ALPACA_DATA_URL=...             # optional override
```

`config_45dte.py` reads these with `os.getenv`; it contains no literal keys.

## Run

```
cd Alpaca/45DTE
python run_45dte.py                  # trade (paper or live per .env)
python run_45dte.py --dry-run        # read from broker, log orders instead of sending them
python run_45dte.py --once           # exit after today's 16:05 end-of-day summary
python run_45dte.py --reset-breaker  # clear the max-loss circuit breaker, then run
```

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
   replace, down to the floor (1.50 / 1.40 strong), then the order is canceled. All
   unfilled entries are canceled at 15:55; no entries are started after that.
5. **Fill -> GTC close.** On fill the actual `filled_avg_price` becomes the entry
   credit and a GTC close is submitted at `round_to_0.05(credit x 0.50)`. Partial fills
   get a close for the filled size; the close is replaced when the rest fills.
6. **Maintenance** every 60 s: detect fills, price reductions, re-create any close order
   that went missing/canceled/rejected/expired, refresh spread price (short mid - long
   mid) for P&L, exit at market when DTE <= 7 or when the spread trades at/beyond 90 %
   of max loss (`MAX_LOSS_EXIT`). Each max-loss hit counts toward the circuit breaker.
7. **Reports** every 60 min and after any fill/close: open positions (right, strikes,
   exp, DTE, qty, entry, current, unrealized, close-order state), working entries,
   closed-today with realized P&L, totals, portfolio risk % of equity, breaker state.
   A `DAILY_LOSS_ALERT` fires if equity is down 3 % from the day's start.
8. **16:05: reconcile + daily summary**, then idle until the next session (or exit with
   `--once`). Weekends and `MARKET_HOLIDAYS` are skipped.

### Circuit breaker

Three spreads hitting max loss (current price >= credit + 0.9 x (5 - credit), or a
realized loss >= 90 % of max loss) trip the breaker: no new entries until you restart
with `--reset-breaker`. Existing positions are still managed. State persists in
`logs/state.json`.

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
cd Alpaca/45DTE
python -m pytest tests -q
```

`tests/fake_broker.py` implements the broker interface in memory; the strategy tests never
touch the network. `tests/live_smoke_alpaca.py` is the broker author's live smoke test.

## Configuration (`config_45dte.py`)

| Knob | Default | Meaning |
| --- | --- | --- |
| `PAPER_TRADING` | from `ALPACA_PAPER` (true) | selects paper vs live base URL |
| `DRY_RUN` | `False` | log order payloads instead of sending (`--dry-run` forces on) |
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
| `EXIT_DTE` | `7` | close at market at or below this DTE |
| `MAX_LOSS_EXIT` | `True` | close at market when a max-loss hit is detected |
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

- `alpaca-reset.py` is a stand-alone helper that cancels all orders and liquidates all
  positions on the paper account; it is not part of the bot.
- `--once` follows the spec literally: run the session and exit after the 16:05 summary.
  Launched outside a session with `--once`, it prints the start-of-day report and exits.
