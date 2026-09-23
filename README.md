# Jeldi-trade

Four self-contained options bots. Each folder runs on its own; the two folders
for a strategy share byte-identical strategy code and differ only in
`broker.py`, the config file, and the login helper.

| Folder | Strategy | Broker | Money |
|---|---|---|---|
| `Alpaca/0DTE` | 0DTE SPX credit spreads (ES overnight breakout + ORB) | Alpaca | paper |
| `Alpaca/45DTE` | 45–60 DTE S&P 500 credit spreads (RSI confluence) | Alpaca | paper |
| `schwab/0dte` | same as Alpaca/0DTE | Schwab | simulated by default (`SCHWAB_MODE=sim`); see below |
| `schwab/45DTE` | same as Alpaca/45DTE | Schwab | simulated by default (`SCHWAB_MODE=sim`); see below |

### Schwab modes

Schwab has no paper-trading API, so the Schwab folders run a local simulator
by default. `SCHWAB_MODE` in the folder's `.env` picks the behaviour:

| Mode | Orders | Data |
|---|---|---|
| `sim` (default) | filled against live Schwab quotes into a simulated account (`logs/sim_state.json`, starting cash `SIM_STARTING_EQUITY`; reset with `python sim_reset.py`) | live Schwab |
| `dry_run` | logged, never sent | live Schwab |
| `live` | **real money** — also requires `SCHWAB_LIVE_ORDERS=true`, otherwise forced back to dry-run | live Schwab |

The simulator fills an opening credit spread when the natural credit
(short bid − long ask) reaches your limit, a closing debit when the natural
debit falls to your limit, marks positions to live mids, cash-settles expired
legs at intrinsic value, and reports equity / P&L / positions exactly like a
real account.

Schwab returns no option chain for `$SPX` on some accounts. If
`ALPACA_API_KEY`/`ALPACA_SECRET_KEY` are present in the Schwab folder's `.env`,
SPX/SPXW quotes and expirations are read from Alpaca's free indicative feed
instead (data only — no Alpaca orders). Without them the 0DTE Schwab bot cannot
price SPX spreads and says so.

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
breakout. After 10:00: ORB only. No new entries after 15:00 ET (2:00 pm CST);
everything is force-closed at 15:30 ET (2:30 pm CST). Bullish → sell an ITM put spread (buy first ITM strike,
sell one width deeper); bearish → sell an ITM call spread. $5-wide below $30k
equity (credit 2.75–3.50), $10-wide above (5.50–7.00); a mid above 3.25 / 6.50
walks both strikes one strike toward spot (max 2, short stays ITM) instead of
entering deep ITM or rejecting. Exit at +$0.30 / −$0.55 (strategy B: −$0.50).
Strategy A: profit lock arms at +$0.30 and exits on a $0.35 giveback (no
candle-against exit); at the target the whole position always becomes the
runner (stop moved to the target level, $0.50 trailing stop, no momentum
slowdown exit). Strategy B: lock arms at +$0.15 / $0.10 giveback with the
candle-against exit, books half at target when momentum continues and runs the
rest with the 20% slowdown exit. A takes both ORB entry kinds (momentum close
and pullback) and, in the first 30 minutes, the overnight high/low run through
the same break/pullback/momentum state machine on 2-minute candles (`ON_BREAK`);
B keeps the original overnight detector. at most 3 trades / 2 consecutive
losses a day, with a 30-minute cool-down after each exit; B keeps the 20 / 5
defaults and both setups. Per-strategy exit knobs live in
`EXIT_TUNING_BY_STRATEGY`, including an optional profit floor by spread width
(`PROFIT_FLOOR_BY_WIDTH`: once `arm` in profit, a hard exit floor at `floor`
profit while the runner keeps going) and a stale timer (`STALE_TIMER_MIN`: in
profit but no target yet after N minutes sets a `STALE_FLOOR` floor). The nine
Alpaca personas test these via `PERSONA_OVERRIDES`; ASTRA is the control. Ctrl+C or SIGTERM closes every open spread before the process exits.
Every trade row records the trigger mid next to the fill (slippage columns).
Per-persona parameter overrides live in `PERSONA_OVERRIDES` at the bottom of
the Alpaca config.

**45DTE** — scans the S&P 500 plus 36 ETFs (broad, sector SPDRs, commodities,
rates, international; `data/sp500_tickers.txt`) every 5 min from
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
