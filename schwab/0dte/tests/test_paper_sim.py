"""Unit tests for the Schwab paper-trading simulator. No network: the data broker is a fake."""

from __future__ import annotations

import logging
import sys
import types
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import broker as broker_mod  # noqa: E402
import paper_sim  # noqa: E402
from broker import Broker, BrokerError, SchwabBroker, occ_symbol  # noqa: E402
from paper_sim import PaperBroker  # noqa: E402

ET = ZoneInfo("US/Eastern")
EXP = date(2026, 9, 17)
LOG = logging.getLogger("test.sim")


def et(h: int, m: int, day: int = 17, month: int = 9) -> datetime:
    return datetime(2026, month, day, h, m, tzinfo=ET)


class FakeData:
    """Controllable chain quotes and spot; counts chain fetches for the cache test."""

    def __init__(self):
        self.quotes: dict[tuple, tuple[float, float]] = {}
        self.spot: dict[str, float] = {"SPX": 6500.0, "AAPL": 200.0}
        self.chain_calls = 0

    def set(self, und: str, exp: date, right: str, strike: float, bid: float, ask: float, root: str | None = None):
        self.quotes[(und, exp, right, strike, root or und)] = (bid, ask)

    def get_option_chain(self, underlying, expiration, right, strike_min=None, strike_max=None, spot=None):
        self.chain_calls += 1
        rows = []
        for (und, exp, r, strike, root), (bid, ask) in self.quotes.items():
            if und != underlying or exp != expiration or r != right:
                continue
            if strike_min is not None and strike < strike_min or strike_max is not None and strike > strike_max:
                continue
            rows.append({"symbol": occ_symbol(root, exp, r, strike), "underlying": und, "root": root, "expiration": exp,
                         "strike": strike, "right": r, "bid": bid, "ask": ask, "mid": round((bid + ask) / 2, 4),
                         "last": None, "delta": None, "iv": None, "quote_time": None})
        return sorted(rows, key=lambda q: q["strike"])

    def get_spot(self, symbol: str) -> float:
        return self.spot[symbol]

    def get_expirations(self, underlying, min_dte=0, max_dte=120):
        return sorted({k[1] for k in self.quotes if k[0] == underlying})


def make_config(tmp_path: Path, **overrides) -> types.ModuleType:
    cfg = types.ModuleType("config_test")
    cfg.LOG_DIR = str(tmp_path / "logs")
    cfg.CLOSE_SLIPPAGE = 0.05
    cfg.CLOSE_RETRY_SEC = 0.01
    cfg.CLOSE_MAX_RETRIES = 2
    cfg.RISK_FREE_RATE = 0.04
    cfg.SIM_STARTING_EQUITY = 2000.0
    cfg.SIM_QUOTE_CACHE_SEC = 10
    cfg.SIM_FILL_START = "09:30"
    cfg.SIM_FILL_END = "16:00"
    cfg.SIM_INDEX_FILL_END = "16:15"
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


class Clock:
    def __init__(self, now: datetime):
        self.now = now


@pytest.fixture
def data() -> FakeData:
    d = FakeData()
    # SPXW 6450/6445 put spread: natural credit = 4.00 - 2.60 = 1.40
    d.set("SPX", EXP, "P", 6450, 4.00, 4.20, root="SPXW")
    d.set("SPX", EXP, "P", 6445, 2.40, 2.60, root="SPXW")
    return d


@pytest.fixture
def clock() -> Clock:
    return Clock(et(10, 0))


@pytest.fixture
def sim(tmp_path, data, clock) -> PaperBroker:
    s = PaperBroker(data, make_config(tmp_path), log=LOG, now_fn=lambda: clock.now)
    s._sleep = lambda _s: None
    return s


def open_spread(sim: PaperBroker, credit: float = 1.40, qty: int = 2, tif: str = "day") -> dict:
    return sim.place_credit_spread("SPX", EXP, "P", 6450, 6445, qty, credit, time_in_force=tif, root="SPXW")


# ---------------------------------------------------------------- credit fills

def test_identity_and_delegation(sim, data):
    assert sim.name == "schwab-sim" and sim.is_paper is True and sim.dry_run is False
    assert sim.get_spot("SPX") == 6500.0
    assert sim.get_expirations("SPX") == [EXP]
    assert not hasattr(sim, "get_expiration_roots")
    assert sim.parse_occ("SPXW260917P06450000")["strike"] == 6450.0
    assert sim.get_account() == {"equity": 2000.0, "cash": 2000.0, "buying_power": 2000.0,
                                 "options_buying_power": 2000.0, "account_id": "SIM"}


def test_credit_order_waits_for_natural_then_fills_at_limit(sim, data):
    order = open_spread(sim, credit=1.50)
    assert order["id"].startswith("SIM-") and order["status"] == "new" and order["order_class"] == "mleg"
    assert [l["position_intent"] for l in order["legs"]] == ["sell_to_open", "buy_to_open"]
    assert [l["symbol"] for l in order["legs"]] == ["SPXW260917P06450000", "SPXW260917P06445000"]
    assert order["time_in_force"] == "day" and order["submitted_at"].tzinfo is not None
    assert sim.get_order(order["id"])["status"] == "new"
    assert sim.get_positions() == []
    assert len(sim.get_open_orders()) == 1

    data.set("SPX", EXP, "P", 6450, 4.30, 4.50, root="SPXW")  # natural 4.30 - 2.60 = 1.70 >= 1.50
    sim._chain_cache.clear()
    filled = sim.wait_for_fill(order["id"], timeout_sec=0.05, poll_sec=0.01)
    assert filled["status"] == "filled" and filled["filled_qty"] == 2
    assert filled["filled_avg_price"] == pytest.approx(1.50)
    assert filled["filled_at"] == et(10, 0)
    short_leg, long_leg = filled["legs"]
    assert short_leg["filled_avg_price"] == pytest.approx(4.30)
    assert long_leg["filled_avg_price"] == pytest.approx(2.80)
    assert short_leg["status"] == long_leg["status"] == "filled"
    assert sim.get_open_orders() == []
    assert sim.state["cash"] == pytest.approx(2000 + 1.50 * 100 * 2)


def test_fill_skipped_when_leg_has_no_quote(sim, data):
    data.set("SPX", EXP, "P", 6445, 0.0, 0.0, root="SPXW")
    order = open_spread(sim, credit=0.10)
    assert sim.get_order(order["id"])["status"] == "new"


EXP2 = date(2026, 9, 25)


def add_exp2(data: FakeData) -> None:
    data.set("SPX", EXP2, "P", 6450, 4.00, 4.20, root="SPXW")
    data.set("SPX", EXP2, "P", 6445, 2.40, 2.60, root="SPXW")


def test_no_fill_outside_regular_hours(sim, data, clock):
    add_exp2(data)
    clock.now = et(9, 15)
    order = sim.place_credit_spread("SPX", EXP2, "P", 6450, 6445, 1, 1.00, time_in_force="gtc", root="SPXW")
    assert sim.get_order(order["id"])["status"] == "new"
    clock.now = et(10, 0, day=19)  # Saturday
    assert sim.get_order(order["id"])["status"] == "new"
    clock.now = et(16, 10, day=21)  # SPX still trades until 16:15
    assert sim.get_order(order["id"])["status"] == "filled"


def test_day_order_expires_at_session_end_and_gtc_persists(sim, data, clock):
    add_exp2(data)
    data.set("AAPL", EXP, "P", 200, 1.0, 1.2)
    data.set("AAPL", EXP, "P", 195, 0.3, 0.4)
    stock = sim.place_credit_spread("AAPL", EXP, "P", 200, 195, 1, 2.00, time_in_force="day")
    index = open_spread(sim, credit=5.00, tif="day")
    gtc = sim.place_credit_spread("SPX", EXP2, "P", 6450, 6445, 1, 5.00, time_in_force="gtc", root="SPXW")
    clock.now = et(15, 59)
    assert {o["id"] for o in sim.get_open_orders()} == {stock["id"], index["id"], gtc["id"]}
    clock.now = et(16, 0)
    assert sim.get_order(stock["id"])["status"] == "expired"
    assert sim.get_order(index["id"])["status"] == "new"
    clock.now = et(16, 15)
    assert sim.get_order(index["id"])["status"] == "expired"
    assert sim.get_order(gtc["id"])["status"] == "new"
    clock.now = et(10, 0, day=18)
    assert sim.get_order(gtc["id"])["status"] == "new"
    clock.now = et(10, 0, day=28)  # legs expired with the contract on 9/25
    assert sim.get_order(gtc["id"])["status"] == "expired"


def test_open_rejected_when_buying_power_insufficient(sim):
    with pytest.raises(BrokerError, match="buying power"):
        open_spread(sim, credit=1.40, qty=6)  # margin (5 - 1.4) * 100 * 6 = 2160 > 2000
    assert sim.get_open_orders() == []
    with pytest.raises(BrokerError):
        open_spread(sim, credit=0.0)
    with pytest.raises(BrokerError):
        sim.place_credit_spread("SPX", EXP, "P", 6450, 6445, 1, 1.0, time_in_force="ioc", root="SPXW")


# ---------------------------------------------------------------- positions / account

def test_positions_equity_and_buying_power(sim, data):
    open_spread(sim, credit=1.40, qty=2)
    positions = sim.get_positions()
    assert [(p["symbol"], p["qty"]) for p in positions] == [("SPXW260917P06445000", 2), ("SPXW260917P06450000", -2)]
    long_leg, short_leg = positions
    assert short_leg["avg_price"] == pytest.approx(4.00) and long_leg["avg_price"] == pytest.approx(2.60)
    assert short_leg["current_price"] == pytest.approx(4.10) and long_leg["current_price"] == pytest.approx(2.50)
    assert short_leg["market_value"] == pytest.approx(-820.0) and long_leg["market_value"] == pytest.approx(500.0)
    assert short_leg["unrealized_pl"] == pytest.approx(-20.0) and long_leg["unrealized_pl"] == pytest.approx(-20.0)
    assert short_leg["expiration"] == EXP and short_leg["right"] == "P" and short_leg["strike"] == 6450.0
    assert short_leg["underlying"] == "SPX" and short_leg["asset_class"] == "option"

    acct = sim.get_account()
    cash = 2000 + 1.40 * 100 * 2
    assert acct["cash"] == pytest.approx(cash)
    assert acct["equity"] == pytest.approx(cash - 820.0 + 500.0)
    margin = (5 - 1.40) * 100 * 2
    assert acct["buying_power"] == pytest.approx(cash - margin)
    assert acct["options_buying_power"] == acct["buying_power"]


def test_positions_fall_back_to_last_mid_when_chain_missing(sim, data):
    open_spread(sim, credit=1.40, qty=1)
    sim.get_positions()  # marks refreshed to the live mids 4.10 / 2.50
    data.quotes.clear()
    sim._chain_cache.clear()
    positions = sim.get_positions()
    assert positions[1]["current_price"] == pytest.approx(4.10)


# ---------------------------------------------------------------- closes

def test_close_order_fill_logic_and_round_trip_pnl(sim, data):
    open_spread(sim, credit=1.40, qty=2)
    with pytest.raises(BrokerError, match="not held"):
        sim.place_close_spread("SPX", EXP, "P", 6450, 6445, 3, 0.70, root="SPXW")
    with pytest.raises(BrokerError):
        sim.place_close_spread("SPX", EXP, "P", 6460, 6455, 1, 0.70, root="SPXW")
    close = sim.place_close_spread("SPX", EXP, "P", 6450, 6445, 2, 0.70, time_in_force="gtc", root="SPXW")
    assert [l["position_intent"] for l in close["legs"]] == ["buy_to_close", "sell_to_close"]
    assert close["status"] == "new"  # natural debit 4.20 - 2.40 = 1.80 > 0.70

    data.set("SPX", EXP, "P", 6450, 2.90, 3.00, root="SPXW")
    data.set("SPX", EXP, "P", 6445, 2.35, 2.45, root="SPXW")  # natural 3.00 - 2.35 = 0.65 <= 0.70
    sim._chain_cache.clear()
    filled = sim.get_order(close["id"])
    assert filled["status"] == "filled" and filled["filled_avg_price"] == pytest.approx(0.70)
    assert filled["legs"][0]["filled_avg_price"] == pytest.approx(3.00)
    assert filled["legs"][1]["filled_avg_price"] == pytest.approx(2.30)
    assert sim.get_positions() == []
    expected = (1.40 - 0.70) * 100 * 2
    assert sim.state["realized_pnl_total"] == pytest.approx(expected)
    assert sim.get_account()["cash"] == pytest.approx(2000 + expected)
    assert sim.get_account()["equity"] == pytest.approx(2000 + expected)
    assert sim.get_account()["buying_power"] == pytest.approx(2000 + expected)


def test_partial_close_keeps_remaining_legs(sim, data):
    open_spread(sim, credit=1.40, qty=3)
    data.set("SPX", EXP, "P", 6450, 2.90, 3.00, root="SPXW")
    data.set("SPX", EXP, "P", 6445, 2.35, 2.45, root="SPXW")
    sim._chain_cache.clear()
    sim.place_close_spread("SPX", EXP, "P", 6450, 6445, 1, 0.70, root="SPXW")
    assert {p["symbol"]: p["qty"] for p in sim.get_positions()} == {"SPXW260917P06450000": -2,
                                                                     "SPXW260917P06445000": 2}
    assert sim.state["realized_pnl_total"] == pytest.approx(70.0)


def test_close_spread_at_market_fills_at_natural_plus_slippage(sim, data):
    open_spread(sim, credit=1.40, qty=2)
    data.set("SPX", EXP, "P", 6450, 3.00, 3.10, root="SPXW")
    data.set("SPX", EXP, "P", 6445, 2.00, 2.10, root="SPXW")  # natural debit 1.10
    sim._chain_cache.clear()
    order = sim.close_spread_at_market("SPX", EXP, "P", 6450, 6445, 2, time_in_force="day", root="SPXW")
    assert order["status"] == "filled" and order["filled_qty"] == 2
    assert order["filled_avg_price"] == pytest.approx(1.15)
    assert order["limit_price"] == pytest.approx(1.15) and order["raw"]["market"] is True
    assert order["legs"][0]["filled_avg_price"] == pytest.approx(3.10)
    assert sim.get_positions() == []
    assert sim.state["realized_pnl_total"] == pytest.approx((1.40 - 1.15) * 100 * 2)
    assert sim.get_order(order["id"])["status"] == "filled"
    with pytest.raises(BrokerError):
        sim.close_spread_at_market("SPX", EXP, "P", 6450, 6445, 1, root="SPXW")


def test_close_spread_at_market_caps_at_width_and_falls_back_to_last_mids(sim, data):
    open_spread(sim, credit=1.40, qty=1)
    data.set("SPX", EXP, "P", 6450, 9.00, 9.50, root="SPXW")
    data.set("SPX", EXP, "P", 6445, 3.00, 3.20, root="SPXW")  # natural 6.50 + slip > width 5
    sim._chain_cache.clear()
    order = sim.close_spread_at_market("SPX", EXP, "P", 6450, 6445, 1, root="SPXW")
    assert order["filled_avg_price"] == pytest.approx(5.00)

    data.set("SPX", EXP, "P", 6450, 4.00, 4.20, root="SPXW")
    data.set("SPX", EXP, "P", 6445, 2.40, 2.60, root="SPXW")
    sim._chain_cache.clear()
    open_spread(sim, credit=1.40, qty=1)
    sim.get_positions()  # last known mids 4.10 / 2.50
    data.quotes.clear()
    sim._chain_cache.clear()
    order = sim.close_spread_at_market("SPX", EXP, "P", 6450, 6445, 1, root="SPXW")
    assert order["status"] == "filled" and order["filled_avg_price"] == pytest.approx(1.60 + 0.05)


# ---------------------------------------------------------------- replace / cancel

def test_replace_creates_new_order_and_marks_old_replaced(sim):
    order = open_spread(sim, credit=1.60)
    new = sim.replace_order_price(order["id"], 1.55)
    assert new["id"] != order["id"] and new["status"] == "new"
    assert new["limit_price"] == pytest.approx(1.55) and new["raw"]["replaces"] == order["id"]
    assert new["legs"] == [dict(l, status="new") for l in order["legs"]]
    assert sim.get_order(order["id"])["status"] == "replaced"
    assert [o["id"] for o in sim.get_open_orders()] == [new["id"]]
    with pytest.raises(BrokerError):
        sim.replace_order_price(order["id"], 1.50)
    with pytest.raises(BrokerError):
        sim.replace_order_price("SIM-nope", 1.50)
    stepped = sim.replace_order_price(new["id"], 1.40)  # natural 1.40: fills on the poll after registering
    assert stepped["status"] == "filled" and stepped["filled_avg_price"] == pytest.approx(1.40)


def test_cancel_and_cancel_all(sim):
    a = open_spread(sim, credit=1.60)
    b = open_spread(sim, credit=1.70, tif="gtc")
    assert sim.cancel_order(a["id"]) is True
    assert sim.get_order(a["id"])["status"] == "canceled"
    assert sim.get_order(a["id"])["legs"][0]["status"] == "canceled"
    assert sim.cancel_order(a["id"]) is False
    assert sim.cancel_order("SIM-missing") is False
    c = open_spread(sim, credit=1.80)
    assert sim.cancel_all_orders() == 2
    assert sim.get_open_orders() == [] and sim.get_order(b["id"])["status"] == "canceled"
    assert sim.get_order(c["id"])["status"] == "canceled"
    assert sim.cancel_all_orders() == 0


def test_wait_for_fill_returns_terminal_immediately_and_raises_on_unknown(sim):
    order = open_spread(sim, credit=1.40)
    assert order["status"] == "filled"
    assert sim.wait_for_fill(order["id"], timeout_sec=5.0)["status"] == "filled"
    with pytest.raises(BrokerError):
        sim.wait_for_fill("SIM-missing", timeout_sec=0.01, poll_sec=0.001)


# ---------------------------------------------------------------- persistence / settlement

def test_state_persists_across_instances(sim, data, tmp_path, clock):
    order = open_spread(sim, credit=1.40, qty=2)
    gtc = open_spread(sim, credit=9.00, tif="gtc")
    assert (tmp_path / "logs" / "sim_state.json").is_file()
    reloaded = PaperBroker(data, make_config(tmp_path), log=LOG, now_fn=lambda: clock.now)
    assert reloaded.get_order(order["id"])["status"] == "filled"
    assert reloaded.get_order(gtc["id"])["status"] == "new"
    assert {p["symbol"]: p["qty"] for p in reloaded.get_positions()} == {"SPXW260917P06450000": -2,
                                                                          "SPXW260917P06445000": 2}
    assert reloaded.get_account() == sim.get_account()
    reloaded.reset(5000)
    assert reloaded.get_account()["equity"] == 5000.0 and reloaded.get_positions() == []
    fresh = PaperBroker(data, make_config(tmp_path), log=LOG, now_fn=lambda: clock.now)
    assert fresh.state["starting_equity"] == 5000.0 and fresh.get_open_orders() == []


def test_expiration_settlement_itm_and_otm(sim, data, clock):
    open_spread(sim, credit=1.40, qty=1)
    cash_after_open = sim.state["cash"]
    data.spot["SPX"] = 6447.0  # short 6450 put ITM by 3, long 6445 put OTM
    clock.now = et(16, 14)
    assert len(sim.get_positions()) == 2
    clock.now = et(16, 15)
    assert sim.get_positions() == []
    assert sim.state["cash"] == pytest.approx(cash_after_open - 3.0 * 100)
    assert sim.state["realized_pnl_total"] == pytest.approx((1.40 - 3.0) * 100)
    assert sim.state["fills"][-1]["kind"] == "settle"

    clock.now = et(10, 0, day=18)
    exp2 = date(2026, 9, 18)
    data.set("SPX", exp2, "P", 6450, 4.00, 4.20, root="SPXW")
    data.set("SPX", exp2, "P", 6445, 2.40, 2.60, root="SPXW")
    sim.place_credit_spread("SPX", exp2, "P", 6450, 6445, 1, 1.40, root="SPXW")
    before = sim.state["realized_pnl_total"]
    data.spot["SPX"] = 6600.0  # both puts expire worthless
    clock.now = et(9, 0, day=21)
    assert sim.get_positions() == []
    assert sim.state["realized_pnl_total"] == pytest.approx(before + 140.0)


# ---------------------------------------------------------------- equity journal

def test_record_daily_equity_and_pnl_summary(sim, tmp_path, clock):
    path = tmp_path / "logs" / "sim_equity_history.csv"
    first = sim.get_pnl_summary()
    assert first == {"ytd": None, "mtd": None, "today": 0.0, "as_of": clock.now}
    sim.record_daily_equity()
    open_spread(sim, credit=1.40, qty=2)  # equity 2000 + (1.40 - 1.60) * 200 = 1960
    assert sim.get_pnl_summary()["today"] == pytest.approx(-40.0)
    sim.record_daily_equity()
    lines = path.read_text().strip().splitlines()
    assert lines == ["date,equity", "2026-09-17,1960.00"]

    path.write_text("date,equity\n2026-01-02,1500.00\n2026-09-01,1900.00\n2026-09-16,1950.00\n2026-09-17,1960.00\n")
    summary = sim.get_pnl_summary()
    assert summary["ytd"] == pytest.approx(460.0)
    assert summary["mtd"] == pytest.approx(60.0)
    assert summary["today"] == pytest.approx(-40.0)  # vs first equity recorded today (2000)

    clock.now = et(9, 45, day=18)  # the 9/17 legs settle worthless (spot 6500): equity 2280 vs prior row 1960
    assert sim.get_pnl_summary()["today"] == pytest.approx(320.0)


def test_chain_cache_reuses_quotes_within_ttl(sim, data):
    sim.get_option_chain("SPX", EXP, "P", strike_min=6440, strike_max=6460)
    sim.get_option_chain("SPX", EXP, "P", strike_min=6440, strike_max=6460)
    assert data.chain_calls == 1
    sim.get_option_chain("SPX", EXP, "P")
    assert data.chain_calls == 2
    rows = sim.get_option_chain("SPX", EXP, "P")
    rows[0]["bid"] = 99.0
    assert sim.get_option_chain("SPX", EXP, "P")[0]["bid"] == 2.40


def test_log_callable_receives_sim_events(tmp_path, data, clock):
    lines: list[str] = []
    s = PaperBroker(data, make_config(tmp_path), log=lines.append, now_fn=lambda: clock.now)
    order = open_spread(s, credit=1.40)
    s.cancel_order(order["id"])
    assert any(l.startswith("SIM ORDER new") for l in lines)
    assert any(l.startswith("SIM FILL") for l in lines)


# ---------------------------------------------------------------- factory

def test_factory_selects_broker_by_mode(tmp_path, monkeypatch):
    monkeypatch.delenv("SCHWAB_LIVE_ORDERS", raising=False)
    sim = Broker(make_config(tmp_path, SCHWAB_MODE="sim"), dry_run=None, log=LOG)
    assert isinstance(sim, PaperBroker) and sim.name == "schwab-sim" and sim.is_paper and not sim.dry_run
    assert isinstance(sim.data, SchwabBroker) and sim.data.dry_run is True
    assert isinstance(Broker(make_config(tmp_path, SCHWAB_MODE="sim"), dry_run=False), PaperBroker)

    flagged = Broker(make_config(tmp_path, SCHWAB_MODE="sim"), dry_run=True, log=LOG)
    assert isinstance(flagged, SchwabBroker) and not isinstance(flagged, PaperBroker) and flagged.dry_run is True

    dry = Broker(make_config(tmp_path, SCHWAB_MODE="dry_run"), dry_run=False)
    assert isinstance(dry, SchwabBroker) and dry.dry_run is True and dry.name == "schwab"
    monkeypatch.setenv("SCHWAB_LIVE_ORDERS", "true")
    assert Broker(make_config(tmp_path, SCHWAB_MODE="dry_run"), dry_run=False).dry_run is True
    assert Broker(make_config(tmp_path, SCHWAB_MODE="sim"), dry_run=True).dry_run is True
    assert Broker(make_config(tmp_path, SCHWAB_MODE="live"), dry_run=False).dry_run is False
    monkeypatch.delenv("SCHWAB_LIVE_ORDERS", raising=False)
    assert Broker(make_config(tmp_path, SCHWAB_MODE="live"), dry_run=False).dry_run is True
    assert Broker(make_config(tmp_path, SCHWAB_MODE="live")).dry_run is True


def test_schwab_broker_accepts_callable_log(tmp_path, monkeypatch):
    monkeypatch.delenv("SCHWAB_LIVE_ORDERS", raising=False)
    lines: list[str] = []
    SchwabBroker._forced_dry_run_warned = False
    b = SchwabBroker(make_config(tmp_path, SCHWAB_MODE="live"), dry_run=False, log=lines.append)
    assert b.dry_run is True and any("FORCING dry_run=True" in l for l in lines)
    assert b.occ_symbol("SPXW", EXP, "P", 6450) == "SPXW260917P06450000"
    assert broker_mod.TERMINAL_STATUSES >= paper_sim.TERMINAL_STATUSES
