"""Manual live smoke test against the ASTRA Alpaca PAPER account. Not collected by pytest.

Run:  python tests/live_smoke_alpaca.py [--steps 1,2,3,4,5]
Places at most 1-contract SPXW spreads and leaves the account flat.
"""

import argparse
import os
import sys
import time
from datetime import datetime, time as dtime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

if __name__ != "__main__":
    raise SystemExit("live_smoke_alpaca.py is a manual script; run it directly")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

os.environ["ACTIVE_TRADER"] = "ASTRA"
os.environ.pop("ALPACA_API_KEY", None)
os.environ.pop("ALPACA_SECRET_KEY", None)

from broker import Broker, BrokerError  # noqa: E402

ET = ZoneInfo("US/Eastern")
CFG = SimpleNamespace(DRY_RUN=False, RISK_FREE_RATE=0.04, CLOSE_SLIPPAGE=0.05, CLOSE_RETRY_SEC=15, CLOSE_MAX_RETRIES=6)

parser = argparse.ArgumentParser()
parser.add_argument("--steps", default="1,2,3,4,5")
args = parser.parse_args()
STEPS = {int(s) for s in args.steps.split(",") if s}

LOGS: list[str] = []


def log(msg: str) -> None:
    LOGS.append(msg)
    print(msg)


def hdr(t: str) -> None:
    print(f"\n===== {t} =====")


def fmt_order(o: dict) -> str:
    legs = ", ".join(f"{l['side']}/{l['position_intent']} {l['symbol']} st={l['status']} fap={l['filled_avg_price']}" for l in o["legs"])
    return (f"id={o['id']} status={o['status']} raw_status={o['raw'].get('status')} class={o['order_class']} "
            f"qty={o['qty']} filled_qty={o['filled_qty']} limit={o['limit_price']} filled_avg_price={o['filled_avg_price']} "
            f"top_raw_fap={o['raw'].get('filled_avg_price')} tif={o['time_in_force']} symbol={o['symbol']!r} legs=[{legs}]")


def market_open_now() -> bool:
    now = datetime.now(ET)
    return now.weekday() < 5 and dtime(9, 30) <= now.time() < dtime(16, 0)


def wait_until_open() -> None:
    for _ in range(50):
        now = datetime.now(ET)
        if now.time() >= dtime(9, 31) and market_open_now():
            return
        print(f"  market not open yet ({now:%H:%M:%S} ET); sleeping 30s")
        time.sleep(30)


b = Broker(CFG, dry_run=False, log=log)
print(f"broker={b.name} paper={b.is_paper} trader={b.trader} base={b.base_url} now_ET={datetime.now(ET):%Y-%m-%d %H:%M:%S}")
today = datetime.now(ET).date()

if 1 in STEPS:
    hdr("1. account / pnl / positions / open orders")
    acct = b.get_account()
    print("get_account:", acct)
    print("get_pnl_summary:", b.get_pnl_summary())
    print("get_positions:", b.get_positions())
    for o in b.get_open_orders():
        print("open order:", fmt_order(o))

if 2 in STEPS:
    hdr("2. expirations")
    exps = b.get_expirations("SPX")
    roots = b.get_expiration_roots("SPX")
    print(f"SPX expirations ({len(exps)}): first 10 -> {exps[:10]}")
    print("roots for first 5:", {d.isoformat(): sorted(roots[d]) for d in exps[:5]})
    assert today in exps, f"today {today} not in SPX expirations"
    print("today present in SPX expirations: OK")
    aapl = b.get_expirations("AAPL", 30, 90)
    print(f"AAPL 30-90 DTE expirations: {aapl}")

spot = None
chain = None
if 3 in STEPS or 4 in STEPS or 5 in STEPS:
    hdr("3. SPXW chain with computed delta")
    spot = b.get_spot("SPX")
    print(f"SPX spot (yfinance): {spot:.2f}")
    chain = b.get_option_chain("SPXW", today, "P", strike_min=spot - 200, strike_max=spot + 200, spot=spot)
    print(f"chain rows: {len(chain)}")
    if chain:
        atm_i = min(range(len(chain)), key=lambda i: abs(chain[i]["strike"] - spot))
        for r in chain[max(0, atm_i - 2): atm_i + 3]:
            print(f"  {r['symbol']} K={r['strike']:.0f} bid={r['bid']:.2f} ask={r['ask']:.2f} mid={r['mid']:.2f} "
                  f"delta={r['delta'] if r['delta'] is None else round(r['delta'], 3)} iv={r['iv'] if r['iv'] is None else round(r['iv'], 3)} t={r['quote_time']}")
        atm = chain[atm_i]
        print(f"ATM put delta {atm['delta']} (expect ~ -0.5): {'OK' if atm['delta'] is not None and -0.7 < atm['delta'] < -0.3 else 'CHECK'}")


def pick_spread(chain_rows: list[dict], target_short: float, min_bid_side: float = 0.10) -> tuple[dict, dict, float] | None:
    by_k = {round(r["strike"], 2): r for r in chain_rows if r["root"] == "SPXW"}
    ks = sorted(k for k in by_k if k <= target_short + 150)
    for k in sorted((k for k in ks if k >= target_short - 5), key=lambda k: abs(k - target_short)):
        s, l = by_k.get(k), by_k.get(k - 5)
        if s and l and s["bid"] > 0 and l["ask"] > 0:
            bid_side = s["bid"] - l["ask"]
            if bid_side >= min_bid_side:
                return s, l, bid_side
    return None


if 4 in STEPS:
    hdr("4. live 1-lot far-OTM SPXW put credit spread: open -> fill -> guaranteed close")
    if not market_open_now():
        wait_until_open()
    spot = b.get_spot("SPX")
    wide = b.get_option_chain("SPXW", today, "P", strike_min=spot - 400, strike_max=spot - 100, spot=spot)
    picked = pick_spread(wide, spot - 300, min_bid_side=0.0)
    if picked is None:
        print("no quoted far-OTM spread found; skipping step 4")
    else:
        s, l, bid_side = picked
        credit = round(max(0.05, bid_side - 0.05), 2)
        print(f"short {s['symbol']} bid/ask {s['bid']}/{s['ask']}  long {l['symbol']} bid/ask {l['bid']}/{l['ask']}  bid-side={bid_side:.2f} -> limit_credit={credit:.2f}")
        o = b.place_credit_spread("SPX", today, "P", s["strike"], l["strike"], 1, credit, time_in_force="day", root="SPXW", client_tag=f"smoke-open-{int(time.time())}")
        print("placed:", fmt_order(o))
        o = b.wait_for_fill(o["id"], 60)
        print("after wait:", fmt_order(o))
        if o["status"] != "filled":
            print("NOT FILLED in 60s -> canceling")
            print("cancel_order ->", b.cancel_order(o["id"]))
            time.sleep(2)
            print("final:", fmt_order(b.get_order(o["id"])))
        else:
            pos = [p for p in b.get_positions() if p["underlying"] == "SPX"]
            print("positions after fill:", pos)
            t0 = time.time()
            c = b.close_spread_at_market("SPX", today, "P", s["strike"], l["strike"], 1, root="SPXW", client_tag=f"smoke-close-{int(time.time())}")
            print(f"close_spread_at_market ({time.time() - t0:.1f}s):", fmt_order(c))
        time.sleep(2)
        left = [p for p in b.get_positions() if p["underlying"] == "SPX"]
        print("SPXW positions remaining:", left, "->", "FLAT OK" if not left else "NOT FLAT")

if 5 in STEPS:
    hdr("5. unfillable gtc spread -> replace_order_price (PATCH on mleg?) -> cancel -> cancel_all")
    # Paper fills quoted mleg orders instantly (apparently at last trade) regardless of limit.
    # Prefer a strike pair with no trade today; otherwise PATCH immediately after POST while pending_new.
    spot = b.get_spot("SPX")
    deep = b.get_option_chain("SPXW", today, "P", strike_min=spot - 900, strike_max=spot - 200, spot=spot)
    no_trade = sorted({r["strike"] for r in deep if r["last"] is None}, reverse=True)
    pair = next(((a, c) for a, c in zip(no_trade, no_trade[1:]) if a - c <= 50), None)
    print(f"deep-OTM rows: {len(deep)}, strikes with no trade today: {no_trade}, pair: {pair}")
    race = pair is None
    if race:
        ks_all = sorted({r["strike"] for r in deep if r["root"] == "SPXW"})
        near = sorted(ks_all, key=lambda k: abs(k - (spot - 300)))
        pair = next(((k, k - 5) for k in near if (k - 5) in ks_all), None)
    if pair is None:
        print("no usable strike pair; skipping PATCH test")
    else:
        ks, kl = pair
        o = b.place_credit_spread("SPX", today, "P", ks, kl, 1, 0.05, time_in_force="gtc", root="SPXW", client_tag=f"smoke-gtc-{int(time.time())}")
        print("placed:", fmt_order(o))
        if not race:
            time.sleep(3)
            cur = b.get_order(o["id"])
            print("state after 3s:", fmt_order(cur))
        else:
            cur = o
        patch_ok = None
        current_id = o["id"]
        if cur["status"] not in ("filled", "partially_filled"):
            try:
                raw = b._t("PATCH", f"/v2/orders/{o['id']}", json={"limit_price": "0.10"})
                print("RAW PATCH on mleg SUCCEEDED:", {k: raw.get(k) for k in ("id", "status", "limit_price", "order_class", "replaces")})
                patch_ok, current_id = True, raw["id"]
            except BrokerError as e:
                print("RAW PATCH on mleg FAILED:", e)
                patch_ok = False
        time.sleep(2)
        cur = b.get_order(current_id)
        print("state:", fmt_order(cur))
        if cur["status"] in ("filled", "partially_filled"):
            print("paper filled it; closing")
            c = b.close_spread_at_market("SPX", today, "P", ks, kl, cur["filled_qty"], root="SPXW")
            print("close ->", fmt_order(c))
        elif cur["status"] not in ("canceled", "rejected", "expired"):
            n = b.replace_order_price(current_id, 0.15)
            print("replace_order_price ->", fmt_order(n))
            time.sleep(2)
            old = b.get_order(current_id)
            print("old order after replace:", None if old is None else fmt_order(old))
            print("cancel_order(new) ->", b.cancel_order(n["id"]))
            time.sleep(2)
            print("new order after cancel:", fmt_order(b.get_order(n["id"])))
        print(f"PATCH on mleg works: {patch_ok}")
    print("cancel_all_orders ->", b.cancel_all_orders())
    time.sleep(2)
    opens = b.get_open_orders()
    stuck = [o for o in opens if o["raw"].get("status") == "pending_cancel"]
    live = [o for o in opens if o not in stuck]
    pos = [p for p in b.get_positions() if p["underlying"] == "SPX"]
    for o in opens:
        print("  remaining:", fmt_order(o))
    print(f"working open orders: {len(live)}  stuck pending_cancel (broker-side): {len(stuck)}  SPXW positions: {pos}")
    print("ACCOUNT FLAT:", "YES" if not live and not pos else "NO")

print("\nbroker log lines:", len(LOGS))
