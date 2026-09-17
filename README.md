# Jeldi-trade

Four self-contained options bots. Each folder runs on its own; the two folders
for a strategy share byte-identical strategy code and differ only in
`broker.py`, the config file, and the login helper.

| Folder | Strategy | Broker | Money |
|---|---|---|---|
| `Alpaca/0DTE` | 0DTE SPX credit spreads (ES overnight breakout + ORB) | Alpaca | paper |
| `Alpaca/45DTE` | 45–60 DTE S&P 500 credit spreads (RSI confluence) | Alpaca | paper |
| `schwab/0dte` | same as Alpaca/0DTE | Schwab | **LIVE** (dry-run until `SCHWAB_LIVE_ORDERS=true`) |
| `schwab/45DTE` | same as Alpaca/45DTE | Schwab | **LIVE** (dry-run until `SCHWAB_LIVE_ORDERS=true`) |

## Setup (per folder)

```bash
cd <folder>
pip install -r requirements.txt
copy .env.example .env      # then fill in keys; .env is git-ignored
```

Schwab folders also need a one-time `python schwab_login.py` to create
`token.json` (git-ignored; the refresh token lasts 7 days).

## Run

```bash
# 0DTE (either broker folder)
python scheduler.py                 # trade
python scheduler.py --dry-run       # full logic, orders logged not sent
python scheduler.py --once          # single tick then exit

# 45DTE (either broker folder)
python run_45dte.py                 # trade
python run_45dte.py --dry-run
python run_45dte.py --reset-breaker # clear the max-loss circuit breaker
```

Both print a start-of-day report (equity, cash, options buying power, YTD /
MTD / today P&L, open orders, open positions, tier) and begin immediately if
launched during the session.

## Strategy summary

**0DTE** — 09:30–09:45: ES overnight high/low (translated to SPX via the
open basis) breakout, confirmed on 2-minute SPX candles (break, then next
candle holds and extends). 09:45–10:00: overnight breakout or opening-range
breakout. After 10:00: ORB only. No new entries after 12:00; everything is
force-closed at 12:30. Bullish → sell an ITM put spread (buy first ITM strike,
sell one width deeper); bearish → sell an ITM call spread. $5-wide below $30k
equity (credit 2.75–3.50), $10-wide above (5.50–7.00). Exit at +$0.30 / −$0.30;
with 2+ contracts and continuing momentum, half closes at target and the rest
runs with the stop moved to the target level, a $0.50 trailing stop, and an
exit on a 20% momentum slowdown.

**45DTE** — scans the S&P 500 (`data/sp500_tickers.txt`) every 5 min from
09:30–11:30, every 30 min until 15:00, then every 5 min to the close. Both
RSI(14) and RSI(28) below 30 → put credit spread; above 70 → call credit
spread. Short strike at 0.30 delta from the live chain, $5 wide, credit ≥ 1.50
(≥ 1.40 when both RSIs are beyond 25/75). On fill a GTC buy-to-close at 50% of
the credit is submitted immediately. Unfilled entries are walked down $0.02/hr
to the floor, then cancelled. Positions are closed at ≤ 7 DTE or at max loss.
Sizing: 4% of equity per trade (1-contract minimum allowed while portfolio risk
stays ≤ 52%), tier caps 3 / 5 / 10 / 15 contracts at <10k / <30k / <50k / 50k+.

All parameters live in each folder's config file. See `docs/BROKER_INTERFACE.md`
for the broker contract and `Alpaca/45DTE/README.md` for the 45DTE knobs.

## Keeping the copies in sync

```bash
python tools/check_sync.py
```

Reports any strategy file or config parameter that differs between the Alpaca
and Schwab copy of a strategy. Edit the Alpaca copy, run its tests, then copy
the changed files over.

## Tests

```bash
cd <folder> && python -m pytest tests
```

`Alpaca/*/tests/live_smoke_alpaca.py` places and closes 1-contract SPXW paper
orders against Alpaca — run it manually only.
