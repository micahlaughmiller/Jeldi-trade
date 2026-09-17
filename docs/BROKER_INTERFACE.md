# Broker interface

Every bot folder contains a `broker.py` exposing a class `Broker`. The strategy
code in a folder only ever talks to `Broker`; it never imports `requests`,
`alpaca_trade_api`, or `schwab` directly. The Alpaca and Schwab `broker.py`
files implement the same methods with the same argument names and return
shapes so the strategy files can be byte-identical across broker folders.

All prices are floats in dollars per share (option premium per contract / 100).
All datetimes returned are timezone-aware in US/Eastern. All methods raise
`BrokerError` (defined in `broker.py`) on a hard failure instead of returning
`None` silently, except where a `None`/empty return is documented below.

## Construction

```python
Broker(config_module, dry_run: bool | None = None, log=None)
```

- `config_module` is the folder's config (`config` or `config_45dte`).
- `dry_run`: if `True`, every order-mutating method (`place_*`, `cancel_*`,
  `close_*`, `replace_*`) logs the exact payload it WOULD send and returns a
  synthetic order dict with `id = "DRY-<uuid>"` and `status = "dry_run"`,
  without contacting the broker. Read methods still hit the broker. If `None`,
  defaults to `config.DRY_RUN` (Alpaca default `False`; Schwab default `True`,
  overridable with env `SCHWAB_LIVE_ORDERS=true`).
- `broker.name` is `"alpaca"` or `"schwab"`; `broker.is_paper` is `True` for
  Alpaca paper, `False` for Schwab (Schwab has no paper API).

## Account

```python
get_account() -> dict
```
Returns `{"equity": float, "cash": float, "buying_power": float,
"options_buying_power": float, "account_id": str}`. `equity` is total
liquidation value. Strategies size on `equity`, never `buying_power`.

```python
get_pnl_summary() -> dict
```
Returns `{"ytd": float | None, "mtd": float | None, "today": float | None,
"as_of": datetime}`. Alpaca: from `/v2/account/portfolio/history`
(equity now minus equity at first trading day of year / month). Schwab: from
the local daily equity journal `logs/equity_history.csv` maintained by
`record_daily_equity()`; `None` when insufficient history. Never raises.

```python
record_daily_equity() -> None
```
Appends `date,equity` to `logs/equity_history.csv` (one row per date; rewrite
today's row if it exists).

## Positions and orders

```python
get_positions() -> list[Position]
```
`Position` is a dict:
`{"symbol": str (OCC or stock), "underlying": str, "qty": int (signed: negative
= short), "avg_price": float, "current_price": float | None, "market_value":
float | None, "unrealized_pl": float | None, "asset_class": "option" |
"stock", "expiration": date | None, "strike": float | None, "right": "P" |
"C" | None}`.

```python
get_open_orders() -> list[Order]
get_order(order_id: str) -> Order | None
```
`Order` is a dict:
`{"id": str, "status": str, "symbol": str, "order_class": "simple" | "mleg",
"side": str | None, "qty": int, "filled_qty": int, "limit_price": float |
None, "filled_avg_price": float | None, "time_in_force": str, "legs":
[{"symbol": str, "side": "buy" | "sell", "position_intent": str, "ratio_qty":
int, "status": str, "filled_avg_price": float | None}], "submitted_at":
datetime, "filled_at": datetime | None, "raw": dict}`.
`status` is normalized to one of: `new`, `accepted`, `pending`, `partially_filled`,
`filled`, `canceled`, `rejected`, `expired`, `replaced`, `dry_run`, `unknown`.
For a multi-leg order `filled_avg_price` is the NET price of the spread
(positive number; the caller knows whether it was a credit or debit).

```python
wait_for_fill(order_id: str, timeout_sec: float, poll_sec: float = 2.0) -> Order
```
Polls `get_order` until status is terminal or timeout; returns the last Order.

## Option chain

```python
get_expirations(underlying: str, min_dte: int = 0, max_dte: int = 120) -> list[date]
```
Real listed expirations for the underlying, ascending. For SPX pass
`underlying="SPX"`; the result includes SPXW (daily/weekly) expirations.

```python
get_option_chain(underlying: str, expiration: date, right: "P" | "C",
                 strike_min: float | None = None, strike_max: float | None = None,
                 spot: float | None = None) -> list[OptionQuote]
```
`OptionQuote` is a dict:
`{"symbol": str (OCC), "underlying": str, "root": str (e.g. "SPXW"),
"expiration": date, "strike": float, "right": "P" | "C", "bid": float, "ask":
float, "mid": float, "last": float | None, "delta": float | None, "iv": float
| None, "quote_time": datetime | None}`.
- Sorted by strike ascending. Entries with `bid == 0 and ask == 0` are dropped.
- `delta` is signed (puts negative). If the broker does not supply greeks
  (Alpaca indicative feed), compute `iv` and `delta` with
  `options_math.implied_vol` / `options_math.bs_delta` from `mid`, `spot`,
  `strike`, time to expiry, and `config.RISK_FREE_RATE`. `spot` must be
  provided by the caller for this; if `spot is None` and the broker has no
  greeks, `delta`/`iv` are `None`.

```python
get_spot(symbol: str) -> float
```
Last trade/quote for a stock or index. Alpaca: stocks via data API
(`feed=iex`); for `"SPX"`/`"^GSPC"` fall back to `market_data` (yfinance)
because Alpaca has no index quotes. Schwab: `get_quote` works for both stocks
and `$SPX`.

## Orders

All order methods take `underlying`, `expiration: date`, `right`, strikes, and
build OCC symbols internally with `occ_symbol(root, expiration, right, strike)`
(zero-padded 8-digit strike ×1000, root e.g. `AAPL` or `SPXW`). For SPX the
strategy passes `root="SPXW"` explicitly via the `root` kwarg; default root is
the underlying.

```python
place_credit_spread(underlying, expiration, right, short_strike, long_strike,
                    qty, limit_credit, time_in_force="gtc", root=None,
                    client_tag=None) -> Order
```
Opens a vertical credit spread as ONE multi-leg order: sell_to_open the short
strike, buy_to_open the long strike, both `ratio_qty=1`, net limit =
`limit_credit`. Never submit the legs as separate orders.

```python
place_close_spread(underlying, expiration, right, short_strike, long_strike,
                   qty, limit_debit, time_in_force="gtc", root=None,
                   client_tag=None) -> Order
```
Closes a credit spread as ONE multi-leg order: buy_to_close the short strike,
sell_to_close the long strike, net limit = `limit_debit` (a debit; the caller
passes a positive number). `qty` may be less than the open size (partial close
for runner mode).

```python
close_spread_at_market(...same args minus limit_debit...) -> Order
```
Same legs, but a marketable close: submit a limit at `ask` of the spread plus
`config.CLOSE_SLIPPAGE` (Alpaca does not allow market orders on mleg); if not
filled within `config.CLOSE_RETRY_SEC`, cancel and resubmit at a worse price,
up to `config.CLOSE_MAX_RETRIES` times. Returns the final Order. This is the
"guaranteed close" path; it must not return until the position is closed or
retries are exhausted (then raise `BrokerError`).

```python
replace_order_price(order_id: str, new_limit: float) -> Order
```
Change the limit price of a working order (PATCH on Alpaca; replace on
Schwab). Returns the new Order (Alpaca returns a new id).

```python
cancel_order(order_id: str) -> bool
cancel_all_orders() -> int
```

## Utilities

```python
occ_symbol(root: str, expiration: date, right: str, strike: float) -> str
parse_occ(symbol: str) -> dict  # {"root","expiration","right","strike"}
```

## Config keys every `broker.py` reads

Alpaca: `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_BASE_URL`,
`ALPACA_DATA_URL`, `DRY_RUN`, `RISK_FREE_RATE`, `CLOSE_SLIPPAGE`,
`CLOSE_RETRY_SEC`, `CLOSE_MAX_RETRIES`, `LOG_DIR`.

Schwab: `SCHWAB_APP_KEY`, `SCHWAB_APP_SECRET`, `SCHWAB_CALLBACK_URL`,
`SCHWAB_TOKEN_PATH`, `SCHWAB_ACCOUNT_INDEX` (0), `DRY_RUN`, `RISK_FREE_RATE`,
`CLOSE_SLIPPAGE`, `CLOSE_RETRY_SEC`, `CLOSE_MAX_RETRIES`, `LOG_DIR`.

Secrets are loaded from the folder's `.env` via `python-dotenv`; config files
must not contain literal keys.
