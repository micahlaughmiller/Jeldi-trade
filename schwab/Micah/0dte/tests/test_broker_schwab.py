"""Unit tests for the Schwab Broker. No network: the schwab client is a fake.

Every test runs with dry_run=True. No order-mutating call ever reaches a client.
"""

from __future__ import annotations

import logging
import sys
import types
from datetime import date, datetime
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import broker as broker_mod  # noqa: E402
from broker import (  # noqa: E402
    Broker,
    BrokerError,
    occ_symbol,
    parse_occ,
    schwab_option_symbol,
    to_occ,
    to_schwab_symbol,
)

EXP = date(2026, 9, 17)


def _resp(status: int = 200, payload=None, headers: dict | None = None) -> httpx.Response:
    if payload is None:
        return httpx.Response(status, headers=headers or {}, request=httpx.Request("GET", "https://x"))
    return httpx.Response(status, json=payload, headers=headers or {}, request=httpx.Request("GET", "https://x"))


class FakeClient:
    """Records every call; raises on any order-mutating call."""

    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []
        self.account_payload = {"securitiesAccount": {
            "accountNumber": "12345678", "type": "MARGIN",
            "currentBalances": {"liquidationValue": 25000.5, "cashBalance": 12000.0, "buyingPower": 50000.0,
                                "buyingPowerNonMarginableTrade": 24000.0, "equity": 25000.5},
            "positions": [
                {"shortQuantity": 2.0, "longQuantity": 0.0, "averagePrice": 9.4, "marketValue": -1880.0,
                 "currentDayProfitLoss": -120.0, "shortOpenProfitLoss": -80.0,
                 "instrument": {"assetType": "OPTION", "symbol": "SPXW  260917P07610000", "putCall": "PUT",
                                "underlyingSymbol": "$SPX"}},
                {"shortQuantity": 0.0, "longQuantity": 2.0, "averagePrice": 7.6, "marketValue": 1500.0,
                 "currentDayProfitLoss": 90.0, "longOpenProfitLoss": -20.0,
                 "instrument": {"assetType": "OPTION", "symbol": "SPXW  260917P07605000", "putCall": "PUT",
                                "underlyingSymbol": "$SPX"}},
                {"shortQuantity": 0.0, "longQuantity": 250.0, "averagePrice": 1.49996, "marketValue": 375.0,
                 "currentDayProfitLoss": 0.0, "longOpenProfitLoss": 0.01,
                 "instrument": {"assetType": "EQUITY", "symbol": "ABCD"}},
                {"shortQuantity": 0.0, "longQuantity": 0.0, "averagePrice": 0.0, "marketValue": 0.0,
                 "instrument": {"assetType": "EQUITY", "symbol": "FLAT"}},
            ]}}
        self.chain_payload = {
            "status": "SUCCESS", "symbol": "$SPX", "underlyingPrice": 7621.76,
            "putExpDateMap": {
                "2026-09-17:0": {
                    "7600.0": [{"putCall": "PUT", "symbol": "SPXW  260917P07600000", "bid": 6.5, "ask": 6.7,
                                "last": 6.6, "mark": 6.6, "delta": -0.31, "volatility": 17.2,
                                "strikePrice": 7600.0, "quoteTimeInLong": 1789652705831}],
                    "7605.0": [{"putCall": "PUT", "symbol": "SPXW  260917P07605000", "bid": 7.7, "ask": 7.9,
                                "last": 7.8, "delta": -0.35, "volatility": 16.9, "strikePrice": 7605.0}],
                    "7610.0": [{"putCall": "PUT", "symbol": "SPXW  260917P07610000", "bid": 9.3, "ask": 9.5,
                                "last": 9.44, "delta": -999.0, "volatility": -999.0, "strikePrice": 7610.0,
                                "quoteTimeInLong": 1789652705831}],
                    "7615.0": [{"putCall": "PUT", "symbol": "SPXW  260917P07615000", "bid": 0.0, "ask": 0.0,
                                "last": None, "delta": -0.44, "volatility": 16.2, "strikePrice": 7615.0}],
                },
                "2026-09-18:1": {
                    "7610.0": [{"putCall": "PUT", "symbol": "SPX   260918P07610000", "bid": 30.0, "ask": 30.5,
                                "delta": -0.4, "volatility": 15.0, "strikePrice": 7610.0}],
                },
            },
            "callExpDateMap": {},
        }
        self.orders_payload: list[dict] = []
        self.order_by_id: dict[str, dict] = {}

    def _rec(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))

    def get_account_numbers(self):
        self._rec("get_account_numbers")
        return _resp(200, [{"accountNumber": "12345678", "hashValue": "HASH0"},
                           {"accountNumber": "87654321", "hashValue": "HASH1"}])

    def get_account(self, account_hash, *, fields=None):
        self._rec("get_account", account_hash, fields=fields)
        return _resp(200, self.account_payload)

    def get_quotes(self, symbols, *, fields=None, indicative=None):
        self._rec("get_quotes", list(symbols))
        return _resp(200, {"$SPX": {"quote": {"lastPrice": 7621.76, "closePrice": 7551.81}},
                           "AAPL": {"quote": {"lastPrice": 0, "bidPrice": 199.9, "askPrice": 200.1}}})

    def get_option_chain(self, symbol, **kwargs):
        self._rec("get_option_chain", symbol, **kwargs)
        return _resp(200, self.chain_payload)

    def get_option_expiration_chain(self, symbol):
        self._rec("get_option_expiration_chain", symbol)
        return _resp(200, {"status": "SUCCESS", "expirationList": [
            {"expirationDate": "2026-09-17", "daysToExpiration": 0, "optionRoots": "SPXW"},
            {"expirationDate": "2026-09-18", "daysToExpiration": 1, "optionRoots": "SPX, SPXW"},
            {"expirationDate": "2027-01-15", "daysToExpiration": 120, "optionRoots": "SPX, SPXW"},
        ]})

    def get_orders_for_account(self, account_hash, **kwargs):
        self._rec("get_orders_for_account", account_hash, **kwargs)
        return _resp(200, self.orders_payload)

    def get_order(self, order_id, account_hash):
        self._rec("get_order", order_id, account_hash)
        raw = self.order_by_id.get(str(order_id))
        return _resp(200, raw) if raw is not None else _resp(404, {"message": "not found"})

    def place_order(self, *a, **k):
        raise AssertionError("place_order must never be called in these tests")

    def replace_order(self, *a, **k):
        raise AssertionError("replace_order must never be called in these tests")

    def cancel_order(self, *a, **k):
        raise AssertionError("cancel_order must never be called in these tests")


def make_config(tmp_path: Path, **overrides) -> types.ModuleType:
    cfg = types.ModuleType("config_test")
    cfg.LOG_DIR = str(tmp_path / "logs")
    cfg.CLOSE_SLIPPAGE = 0.05
    cfg.CLOSE_RETRY_SEC = 0.01
    cfg.CLOSE_MAX_RETRIES = 2
    cfg.RISK_FREE_RATE = 0.04
    cfg.SCHWAB_ACCOUNT_FILE = str(tmp_path / "selected_account.json")      # never the real folder's selection
    cfg.SCHWAB_ACCOUNT_INDEX = 0                                           # nor whatever this machine's environment says
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


@pytest.fixture
def fake() -> FakeClient:
    return FakeClient()


@pytest.fixture
def brk(tmp_path, fake, monkeypatch) -> Broker:
    monkeypatch.delenv("SCHWAB_LIVE_ORDERS", raising=False)
    b = Broker(make_config(tmp_path), dry_run=True, log=logging.getLogger("test.broker"))
    b._client = fake
    b._sleep = lambda s: None
    return b


# ---------------------------------------------------------------- symbols

def test_schwab_symbol_format_and_roundtrip():
    assert schwab_option_symbol("SPXW", EXP, "P", 7510) == "SPXW  260917P07510000"
    assert occ_symbol("SPXW", EXP, "P", 7510) == "SPXW260917P07510000"
    assert to_schwab_symbol("SPXW260917P07510000") == "SPXW  260917P07510000"
    assert to_occ("SPXW  260917P07510000") == "SPXW260917P07510000"
    assert schwab_option_symbol("AAPL", date(2026, 11, 20), "C", 150.5) == "AAPL  261120C00150500"
    assert len(schwab_option_symbol("SPX", EXP, "C", 7500)) == 21
    assert schwab_option_symbol("SPX", EXP, "C", 7500).startswith("SPX   ")


def test_parse_occ_handles_both_forms():
    for sym in ("SPXW260917P07510000", "SPXW  260917P07510000", "spxw  260917p07510000"):
        p = parse_occ(sym)
        assert p == {"root": "SPXW", "expiration": EXP, "right": "P", "strike": 7510.0}
    assert parse_occ("AAPL261120C00150500")["strike"] == 150.5
    with pytest.raises(BrokerError):
        parse_occ("AAPL")
    with pytest.raises(BrokerError):
        parse_occ("$SPX")


# ---------------------------------------------------------------- safety

def test_dry_run_defaults_true_and_env_gate(tmp_path, monkeypatch):
    monkeypatch.delenv("SCHWAB_LIVE_ORDERS", raising=False)
    assert Broker(make_config(tmp_path)).dry_run is True
    assert Broker(make_config(tmp_path, DRY_RUN=False)).dry_run is True
    assert Broker(make_config(tmp_path), dry_run=False).dry_run is True
    monkeypatch.setenv("SCHWAB_LIVE_ORDERS", "true")
    assert Broker(make_config(tmp_path), dry_run=True).dry_run is True
    assert Broker(make_config(tmp_path, DRY_RUN=True)).dry_run is True
    assert Broker(make_config(tmp_path), dry_run=False).dry_run is False
    b = Broker(make_config(tmp_path))
    assert b.name == "schwab" and b.is_paper is False


def test_forced_dry_run_warns(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("SCHWAB_LIVE_ORDERS", raising=False)
    Broker._forced_dry_run_warned = False
    with caplog.at_level(logging.WARNING):
        Broker(make_config(tmp_path), dry_run=False)
    assert any("FORCING dry_run=True" in r.message for r in caplog.records)


# ---------------------------------------------------------------- order json

def test_place_credit_spread_dry_run_json(brk, fake):
    order = brk.place_credit_spread("SPX", EXP, "P", 7610, 7605, qty=2, limit_credit=1.5, root="SPXW")
    assert order["id"].startswith("DRY-") and order["status"] == "dry_run"
    assert order["order_class"] == "mleg" and order["symbol"] == "SPX"
    assert order["qty"] == 2 and order["limit_price"] == 1.5 and order["time_in_force"] == "gtc"
    assert [l["symbol"] for l in order["legs"]] == ["SPXW260917P07610000", "SPXW260917P07605000"]
    assert [l["side"] for l in order["legs"]] == ["sell", "buy"]
    assert [l["position_intent"] for l in order["legs"]] == ["sell_to_open", "buy_to_open"]
    raw = order["raw"]
    assert raw == {
        "orderType": "NET_CREDIT", "session": "NORMAL", "price": "1.50", "duration": "GOOD_TILL_CANCEL",
        "orderStrategyType": "SINGLE", "complexOrderStrategyType": "VERTICAL",
        "orderLegCollection": [
            {"instruction": "SELL_TO_OPEN", "quantity": 2,
             "instrument": {"symbol": "SPXW  260917P07610000", "assetType": "OPTION"}},
            {"instruction": "BUY_TO_OPEN", "quantity": 2,
             "instrument": {"symbol": "SPXW  260917P07605000", "assetType": "OPTION"}},
        ]}
    assert fake.calls == []
    assert brk.get_order(order["id"]) is order


def test_place_close_spread_dry_run_json(brk, fake):
    order = brk.place_close_spread("AAPL", date(2026, 11, 20), "P", 150, 145, qty=1, limit_debit=0.75,
                                   time_in_force="day")
    raw = order["raw"]
    assert raw["orderType"] == "NET_DEBIT" and raw["price"] == "0.75" and raw["duration"] == "DAY"
    assert raw["orderLegCollection"] == [
        {"instruction": "BUY_TO_CLOSE", "quantity": 1,
         "instrument": {"symbol": "AAPL  261120P00150000", "assetType": "OPTION"}},
        {"instruction": "SELL_TO_CLOSE", "quantity": 1,
         "instrument": {"symbol": "AAPL  261120P00145000", "assetType": "OPTION"}},
    ]
    assert order["symbol"] == "AAPL" and order["time_in_force"] == "day"
    assert fake.calls == []


def test_order_validation(brk):
    with pytest.raises(BrokerError):
        brk.place_credit_spread("SPX", EXP, "P", 7610, 7605, qty=0, limit_credit=1.5, root="SPXW")
    with pytest.raises(BrokerError):
        brk.place_credit_spread("SPX", EXP, "P", 7610, 7605, qty=1, limit_credit=0, root="SPXW")
    with pytest.raises(BrokerError):
        brk.place_credit_spread("SPX", EXP, "P", 7610, 7605, qty=1, limit_credit=1, time_in_force="ioc", root="SPXW")


def test_dry_run_cancel_replace_and_wait(brk, fake):
    order = brk.place_credit_spread("SPX", EXP, "P", 7610, 7605, qty=1, limit_credit=1.5, root="SPXW")
    waited = brk.wait_for_fill(order["id"], timeout_sec=1.0, poll_sec=0.01)
    assert waited["status"] == "dry_run"
    replaced = brk.replace_order_price(order["id"], 1.40)
    assert replaced["id"].startswith("DRY-") and replaced["raw"]["price"] == "1.40"
    assert replaced["raw"]["orderLegCollection"] == order["raw"]["orderLegCollection"]
    assert brk.get_order(order["id"])["status"] == "replaced"
    assert brk.cancel_order(replaced["id"]) is True
    assert brk.get_order(replaced["id"])["status"] == "canceled"
    assert brk.cancel_order("DRY-nonexistent") is False
    assert brk.cancel_order("1234567890") is True
    assert not any(name in ("place_order", "replace_order", "cancel_order") for name, _, _ in fake.calls)


def test_close_spread_at_market_dry_run_prices_from_chain(brk, fake):
    order = brk.close_spread_at_market("SPX", EXP, "P", 7610, 7605, qty=1, root="SPXW")
    assert order["status"] == "dry_run"
    assert order["raw"]["orderType"] == "NET_DEBIT"
    assert order["raw"]["price"] == "1.85"
    assert [c[0] for c in fake.calls] == ["get_option_chain"]


# ---------------------------------------------------------------- normalization

RAW_FILLED = {
    "orderId": 1004055538123, "status": "FILLED", "orderType": "NET_CREDIT", "price": 1.5, "quantity": 2.0,
    "filledQuantity": 2.0, "duration": "GOOD_TILL_CANCEL", "session": "NORMAL", "orderStrategyType": "SINGLE",
    "complexOrderStrategyType": "VERTICAL", "enteredTime": "2026-09-17T14:31:02+0000",
    "closeTime": "2026-09-17T14:32:10+0000",
    "orderLegCollection": [
        {"legId": 1, "instruction": "SELL_TO_OPEN", "quantity": 2.0, "positionEffect": "OPENING",
         "instrument": {"assetType": "OPTION", "symbol": "SPXW  260917P07610000", "putCall": "PUT",
                        "underlyingSymbol": "$SPX"}},
        {"legId": 2, "instruction": "BUY_TO_OPEN", "quantity": 2.0, "positionEffect": "OPENING",
         "instrument": {"assetType": "OPTION", "symbol": "SPXW  260917P07605000", "putCall": "PUT",
                        "underlyingSymbol": "$SPX"}},
    ],
    "orderActivityCollection": [
        {"activityType": "EXECUTION", "executionType": "FILL", "quantity": 1.0,
         "executionLegs": [{"legId": 1, "quantity": 1.0, "price": 9.40}, {"legId": 2, "quantity": 1.0, "price": 7.85}]},
        {"activityType": "EXECUTION", "executionType": "FILL", "quantity": 1.0,
         "executionLegs": [{"legId": 1, "quantity": 1.0, "price": 9.50}, {"legId": 2, "quantity": 1.0, "price": 7.85}]},
    ],
}


def test_normalize_filled_vertical_order(brk):
    o = brk._normalize_order(RAW_FILLED)
    assert o["id"] == "1004055538123" and o["status"] == "filled"
    assert o["order_class"] == "mleg" and o["side"] is None and o["symbol"] == "SPX"
    assert o["qty"] == 2 and o["filled_qty"] == 2 and o["limit_price"] == 1.5
    assert o["time_in_force"] == "gtc"
    assert o["filled_avg_price"] == pytest.approx(1.60)
    assert [l["symbol"] for l in o["legs"]] == ["SPXW260917P07610000", "SPXW260917P07605000"]
    assert o["legs"][0]["filled_avg_price"] == pytest.approx(9.45)
    assert o["legs"][1]["filled_avg_price"] == pytest.approx(7.85)
    assert o["legs"][0]["ratio_qty"] == 1 and o["legs"][0]["position_intent"] == "sell_to_open"
    assert o["submitted_at"].tzinfo is not None and o["submitted_at"].hour == 10
    assert o["submitted_at"].tzname() == "EDT"
    assert o["filled_at"] is not None and o["raw"] is RAW_FILLED


@pytest.mark.parametrize("raw_status,filled,expected", [
    ("WORKING", 0.0, "new"), ("ACCEPTED", 0.0, "accepted"), ("QUEUED", 0.0, "pending"),
    ("PENDING_ACTIVATION", 0.0, "pending"), ("WORKING", 1.0, "partially_filled"), ("CANCELED", 0.0, "canceled"),
    ("REJECTED", 0.0, "rejected"), ("EXPIRED", 0.0, "expired"), ("REPLACED", 0.0, "replaced"),
    ("SOMETHING_NEW", 0.0, "unknown"),
])
def test_status_normalization(brk, raw_status, filled, expected):
    raw = dict(RAW_FILLED, status=raw_status, filledQuantity=filled, orderActivityCollection=[])
    o = brk._normalize_order(raw)
    assert o["status"] == expected
    assert o["filled_avg_price"] is None
    assert o["filled_at"] is None


def test_get_open_orders_filters_terminal(brk, fake):
    fake.orders_payload = [
        dict(RAW_FILLED, orderId=1, status="WORKING", filledQuantity=0.0, orderActivityCollection=[]),
        dict(RAW_FILLED, orderId=2, status="FILLED"),
        dict(RAW_FILLED, orderId=3, status="CANCELED", orderActivityCollection=[]),
        dict(RAW_FILLED, orderId=4, status="QUEUED", filledQuantity=0.0, orderActivityCollection=[]),
    ]
    ids = [o["id"] for o in brk.get_open_orders()]
    assert ids == ["1", "4"]
    name, args, kwargs = fake.calls[-1]
    assert name == "get_orders_for_account" and args == ("HASH0",)
    assert kwargs["from_entered_datetime"] < kwargs["to_entered_datetime"]


def test_get_order_live_lookup_and_404(brk, fake):
    fake.order_by_id["1004055538123"] = RAW_FILLED
    assert brk.get_order("1004055538123")["status"] == "filled"
    assert brk.get_order("999") is None
    assert brk.cancel_all_orders() == 0


def test_positions_normalization(brk, fake):
    positions = brk.get_positions()
    assert len(positions) == 3
    short_put, long_put, stock = positions
    assert short_put["symbol"] == "SPXW260917P07610000" and short_put["qty"] == -2
    assert short_put["underlying"] == "SPX" and short_put["asset_class"] == "option"
    assert short_put["expiration"] == EXP and short_put["strike"] == 7610.0 and short_put["right"] == "P"
    assert short_put["avg_price"] == 9.4 and short_put["market_value"] == -1880.0
    assert short_put["current_price"] == pytest.approx(9.4)
    assert short_put["unrealized_pl"] == -80.0
    assert long_put["qty"] == 2 and long_put["current_price"] == pytest.approx(7.5)
    assert stock["symbol"] == "ABCD" and stock["asset_class"] == "stock" and stock["qty"] == 250
    assert stock["underlying"] == "ABCD" and stock["expiration"] is None and stock["right"] is None
    assert stock["current_price"] == pytest.approx(1.5)
    name, args, kwargs = fake.calls[-1]
    assert name == "get_account" and args == ("HASH0",)
    assert kwargs["fields"] == broker_mod.SchwabClient.Account.Fields.POSITIONS


def test_get_account_and_index(brk, fake, tmp_path, monkeypatch):
    acct = brk.get_account()
    assert acct == {"equity": 25000.5, "cash": 12000.0, "buying_power": 50000.0,
                    "options_buying_power": 24000.0, "account_id": "12345678"}
    monkeypatch.delenv("SCHWAB_LIVE_ORDERS", raising=False)
    b2 = Broker(make_config(tmp_path, SCHWAB_ACCOUNT_INDEX=1))
    b2._client = fake
    assert b2._hash() == "HASH1"
    b3 = Broker(make_config(tmp_path, SCHWAB_ACCOUNT_INDEX=5))
    b3._client = fake
    with pytest.raises(BrokerError):
        b3._hash()


# ---------------------------------------------------------------- chain / quotes

def test_chain_normalization(brk, fake):
    rows = brk.get_option_chain("SPX", EXP, "P")
    assert [r["strike"] for r in rows] == [7600.0, 7605.0, 7610.0]
    r0 = rows[0]
    assert r0["symbol"] == "SPXW260917P07600000" and r0["root"] == "SPXW" and r0["underlying"] == "SPX"
    assert r0["expiration"] == EXP and r0["right"] == "P"
    assert r0["bid"] == 6.5 and r0["ask"] == 6.7 and r0["mid"] == pytest.approx(6.6) and r0["last"] == 6.6
    assert r0["delta"] == -0.31 and r0["iv"] == pytest.approx(0.172)
    assert isinstance(r0["quote_time"], datetime) and r0["quote_time"].tzinfo is not None
    r_missing_greeks = rows[2]
    assert r_missing_greeks["delta"] is not None and -1.0 < r_missing_greeks["delta"] < 0.0
    assert r_missing_greeks["iv"] is not None and r_missing_greeks["iv"] > 0
    name, args, kwargs = fake.calls[-1]
    assert name == "get_option_chain" and args == ("$SPX",)
    assert kwargs["contract_type"] == broker_mod.SchwabClient.Options.ContractType.PUT
    assert kwargs["from_date"] == EXP and kwargs["to_date"] == EXP


def test_chain_strike_filter(brk):
    rows = brk.get_option_chain("SPX", EXP, "P", strike_min=7605, strike_max=7605)
    assert [r["strike"] for r in rows] == [7605.0]
    assert brk.get_option_chain("SPX", date(2026, 9, 19), "P") == []
    with pytest.raises(BrokerError):
        brk.get_option_chain("SPX", EXP, "X")


def test_get_expirations(brk, fake, monkeypatch):
    class FixedDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 17, 10, 0, tzinfo=tz)
    monkeypatch.setattr(broker_mod, "datetime", FixedDT)
    assert brk.get_expirations("SPX", 0, 30) == [date(2026, 9, 17), date(2026, 9, 18)]
    assert brk.get_expirations("SPX", 1, 200) == [date(2026, 9, 18), date(2027, 1, 15)]
    assert fake.calls[-1][0] == "get_option_expiration_chain" and fake.calls[-1][1] == ("$SPX",)


def test_get_spot(brk, fake):
    assert brk.get_spot("SPX") == 7621.76
    assert brk.get_spot("^GSPC") == 7621.76
    assert fake.calls[-1] == ("get_quotes", (["$SPX"],), {})
    assert brk.get_spot("AAPL") == pytest.approx(200.0)


# ---------------------------------------------------------------- pnl journal

def test_equity_history_and_pnl(brk, fake, tmp_path):
    path = Path(brk.log_dir) / "equity_history.csv"
    empty = brk.get_pnl_summary()
    assert empty["ytd"] is None and empty["mtd"] is None and empty["today"] == pytest.approx(-30.0)
    assert empty["as_of"].tzinfo is not None
    brk.record_daily_equity()
    brk.record_daily_equity()
    lines = path.read_text().strip().splitlines()
    assert lines[0] == "date,equity" and len(lines) == 2 and lines[1].endswith(",25000.50")
    today = datetime.now(broker_mod.ET).date()
    path.write_text(f"date,equity\n{today.year}-01-02,20000.00\n{today.year}-{today.month:02d}-01,24000.00\n"
                    f"{today.isoformat()},25000.50\n")
    summary = brk.get_pnl_summary()
    if today.day > 1:
        assert summary["mtd"] == pytest.approx(1000.5)
    if today != date(today.year, 1, 2):
        assert summary["ytd"] == pytest.approx(5000.5)


def test_pnl_summary_never_raises(brk, fake):
    def boom(*a, **k):
        raise RuntimeError("network down")
    fake.get_account = boom
    summary = brk.get_pnl_summary()
    assert summary["ytd"] is None and summary["today"] is None


# ---------------------------------------------------------------- transport

def test_request_retries_then_raises(brk):
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        return _resp(503, {"error": "busy"})
    with pytest.raises(BrokerError):
        brk._request(flaky)
    assert attempts["n"] == 3

    def bad():
        return _resp(400, {"message": "bad"})
    with pytest.raises(BrokerError, match="HTTP 400"):
        brk._request(bad)

    def transport_error():
        raise httpx.ConnectError("boom")
    with pytest.raises(BrokerError, match="3 attempts"):
        brk._request(transport_error)


def test_order_id_from_location_header():
    resp = _resp(201, headers={"Location": "https://api.schwabapi.com/trader/v1/accounts/HASH0/orders/1004055538123"})
    assert Broker._order_id_from_response(resp) == "1004055538123"
    assert Broker._order_id_from_response(_resp(201)) is None


# ------------------------------------------------- token failures are BrokerErrors, fail fast, and heal (2026-10-08)

class _OAuthError(Exception):
    __module__ = "authlib.integrations.base_client.errors"


def test_a_dead_refresh_token_becomes_a_schwab_auth_error_not_a_crash(brk):
    def refresh_fails(*a, **k):
        raise _OAuthError("invalid_grant: Refresh token is invalid, expired or revoked")
    with pytest.raises(broker_mod.SchwabAuthError) as ei:
        brk._request(refresh_fails)
    assert isinstance(ei.value, BrokerError) and "re-login" in str(ei.value) and "schwab_login.py --force" in str(ei.value)


def test_a_non_json_token_response_is_also_an_auth_failure(brk):
    def bad_body(*a, **k):
        raise ValueError("Expecting value: line 1 column 1 (char 0)")
    with pytest.raises(broker_mod.SchwabAuthError):
        brk._request(bad_body)


def test_http_401_is_an_auth_error_and_other_errors_are_not(brk):
    with pytest.raises(broker_mod.SchwabAuthError, match="HTTP 401"):
        brk._request(lambda: _resp(401, {"error": "unauthorized"}))
    brk._auth_dead_until = 0.0
    with pytest.raises(BrokerError) as ei:
        brk._request(lambda: _resp(400, {"error": "bad"}))
    assert not isinstance(ei.value, broker_mod.SchwabAuthError)


def test_an_unexpected_exception_is_a_plain_broker_error(brk):
    def broken(*a, **k):
        raise KeyError("some bug")
    with pytest.raises(BrokerError) as ei:
        brk._request(broken)
    assert not isinstance(ei.value, broker_mod.SchwabAuthError) and "KeyError" in str(ei.value)


def test_after_an_auth_failure_schwab_is_not_called_again_until_the_probe_window_passes(brk, monkeypatch):
    calls = []

    def refresh_fails(*a, **k):
        calls.append(1)
        raise _OAuthError("invalid_grant")
    clock = {"t": 1000.0}
    monkeypatch.setattr(broker_mod.time, "monotonic", lambda: clock["t"])
    with pytest.raises(broker_mod.SchwabAuthError):
        brk._request(refresh_fails)
    assert len(calls) == 1
    for _ in range(5):                                                       # a dead token costs nothing per call now
        with pytest.raises(broker_mod.SchwabAuthError, match="next attempt in"):
            brk._request(refresh_fails)
    assert len(calls) == 1
    clock["t"] += 61                                                         # probe window over: one real attempt
    with pytest.raises(broker_mod.SchwabAuthError):
        brk._request(refresh_fails)
    assert len(calls) == 2


def test_a_successful_call_clears_the_dead_flag(brk, monkeypatch):
    monkeypatch.setattr(broker_mod.time, "monotonic", lambda: 5000.0)
    brk._auth_dead_until = 4000.0                                            # already past
    assert brk._request(lambda: _resp(200, {"ok": 1})).status_code == 200 and brk._auth_dead_until == 0.0


def test_a_replaced_token_file_reloads_the_client_without_a_restart(tmp_path, monkeypatch):
    import json
    cfg = make_config(tmp_path)
    monkeypatch.setenv("SCHWAB_APP_KEY", "k")
    monkeypatch.setenv("SCHWAB_APP_SECRET", "s")
    b = Broker(cfg, dry_run=True, log=logging.getLogger("t"))
    sb = getattr(b, "data", b)
    token = tmp_path / "t.json"
    sb.token_path = token
    token.write_text(json.dumps({"creation_timestamp": 1_000_000.0, "token": {}}))
    built = []
    monkeypatch.setattr(broker_mod, "client_from_token_file", lambda *a, **k: built.append(a) or object())
    first = sb._get_client()
    assert sb._get_client() is first and len(built) == 1                     # same login: the client is reused
    token.write_text(json.dumps({"creation_timestamp": 2_000_000.0, "token": {}}))     # schwab_login.py ran again
    assert sb._get_client() is not first and len(built) == 2


def test_token_status_reads_this_bots_token_and_its_hard_stop(tmp_path, monkeypatch):
    import json
    from datetime import timedelta
    cfg = make_config(tmp_path)
    b = Broker(cfg, dry_run=True, log=logging.getLogger("t"))
    sb = getattr(b, "data", b)
    sb.token_path = tmp_path / "t.json"
    assert sb.token_status()["state"] == "MISSING"
    created = datetime.now(broker_mod.ET) - timedelta(days=5, hours=1)
    sb.token_path.write_text(json.dumps({"creation_timestamp": created.timestamp(), "token": {}}))
    st = sb.token_status()
    assert st["state"] in ("WARN", "EXPIRING") and "schwab_login.py --force" in st["relogin"]
    if hasattr(b, "token_status"):
        assert b.token_status()["state"] == st["state"]                      # the simulated broker reports its data broker's token


# ------------------------------------------------- 2026-10-12: which token file? (a folder path, and the file named in every auth error)

def test_a_token_path_that_names_a_folder_means_the_token_json_inside_it(tmp_path, monkeypatch):
    folder = tmp_path / "0dte"
    folder.mkdir()
    cfg = make_config(tmp_path)
    cfg.SCHWAB_TOKEN_PATH = str(folder)
    b = Broker(cfg, dry_run=True, log=logging.getLogger("t"))
    sb = getattr(b, "data", b)
    assert sb.token_path == folder / "token.json"
    cfg.SCHWAB_TOKEN_PATH = str(tmp_path / "elsewhere.json")                  # a file path (existing or not) is left alone
    b = Broker(cfg, dry_run=True, log=logging.getLogger("t"))
    assert getattr(b, "data", b).token_path == tmp_path / "elsewhere.json"


def test_every_auth_error_names_the_token_file_it_read(brk, monkeypatch):
    brk.token_path = Path("C:/bots/schwab/45DTE/token.json")
    clock = {"t": 1000.0}
    monkeypatch.setattr(broker_mod.time, "monotonic", lambda: clock["t"])

    def refresh_fails(*a, **k):
        raise _OAuthError("invalid_grant")
    with pytest.raises(broker_mod.SchwabAuthError) as first:
        brk._request(refresh_fails)
    with pytest.raises(broker_mod.SchwabAuthError) as skipped:
        brk._request(refresh_fails)
    for err in (first.value, skipped.value):
        assert str(brk.token_path) in str(err) and "token file:" in str(err) and "schwab_login.py --force" in str(err)


def test_the_login_script_writes_the_token_json_inside_a_folder_path(tmp_path, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("schwab_login_under_test", Path(__file__).resolve().parents[1] / "schwab_login.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    folder = tmp_path / "0dte"
    folder.mkdir()
    monkeypatch.setenv("SCHWAB_TOKEN_PATH", str(folder))
    assert module._token_path() == folder / "token.json"
    monkeypatch.setenv("SCHWAB_TOKEN_PATH", str(tmp_path / "x.json"))
    assert module._token_path() == tmp_path / "x.json"


# ------------------------------------------------- 2026-10-14: the Schwab bots share the NEWEST login (a new login cancels the older one)

def _two_bots(tmp_path, monkeypatch, own_days_old=None, sibling_days_old=None, sibling_has_login_script=True):
    """tmp/schwab/45DTE (this bot) and tmp/schwab/0dte (its sibling), each with a token.json of the given age in days (None = no file)."""
    import json
    import time
    root = tmp_path / "schwab"
    me, other = root / "45DTE", root / "0dte"
    for folder in (me, other):
        folder.mkdir(parents=True)
    (me / "schwab_login.py").write_text("")
    if sibling_has_login_script:
        (other / "schwab_login.py").write_text("")

    def login(folder, days_old):
        if days_old is not None:
            (folder / "token.json").write_text(json.dumps({"creation_timestamp": int(time.time() - days_old * 86400), "token": {}}))
    login(me, own_days_old)
    login(other, sibling_days_old)
    monkeypatch.setattr(broker_mod, "HERE", me)
    monkeypatch.setenv("SCHWAB_APP_KEY", "k")
    monkeypatch.setenv("SCHWAB_APP_SECRET", "s")
    monkeypatch.delenv("SCHWAB_TOKEN_PATH", raising=False)
    built = []
    monkeypatch.setattr(broker_mod, "client_from_token_file", lambda path, *a, **k: built.append(path) or object())
    b = Broker(make_config(tmp_path), dry_run=True, log=logging.getLogger("t"))
    return getattr(b, "data", b), me, other, built, login


def test_a_newer_login_in_the_sibling_bots_folder_is_used(tmp_path, monkeypatch, caplog):
    caplog.set_level("INFO")
    sb, me, other, built, _ = _two_bots(tmp_path, monkeypatch, own_days_old=3, sibling_days_old=0.1)
    sb._get_client()
    assert built == [str(other / "token.json")] and sb.token_path == other / "token.json"
    assert any("NEWER login" in r.getMessage() and str(other / "token.json") in r.getMessage() for r in caplog.records)


def test_its_own_login_is_used_when_it_is_newer_or_equal(tmp_path, monkeypatch):
    sb, me, other, built, _ = _two_bots(tmp_path, monkeypatch, own_days_old=0.1, sibling_days_old=3)
    sb._get_client()
    assert built == [str(me / "token.json")]
    sb, me, other, built, _ = _two_bots(tmp_path / "tie", monkeypatch, own_days_old=1, sibling_days_old=1)
    sb._get_client()
    assert built[0] in (str(me / "token.json"), str(other / "token.json"))        # equal to within the second: either is the same login age


def test_the_siblings_login_is_used_when_this_bot_has_none(tmp_path, monkeypatch):
    sb, me, other, built, _ = _two_bots(tmp_path, monkeypatch, own_days_old=None, sibling_days_old=2)
    sb._get_client()
    assert built == [str(other / "token.json")]


def test_with_no_login_anywhere_the_error_names_this_bots_own_file(tmp_path, monkeypatch):
    sb, me, other, built, _ = _two_bots(tmp_path, monkeypatch, own_days_old=None, sibling_days_old=None)
    with pytest.raises(BrokerError, match="token file not found") as err:
        sb._get_client()
    assert str(me / "token.json") in str(err.value) and built == []


def test_a_folder_without_its_own_login_script_is_not_a_sibling_bot(tmp_path, monkeypatch):
    sb, me, other, built, _ = _two_bots(tmp_path, monkeypatch, own_days_old=3, sibling_days_old=0.1, sibling_has_login_script=False)
    sb._get_client()
    assert built == [str(me / "token.json")]


def test_an_explicit_token_path_is_used_exactly_as_given(tmp_path, monkeypatch):
    sb, me, other, built, login = _two_bots(tmp_path, monkeypatch, own_days_old=3, sibling_days_old=0.1)
    monkeypatch.setenv("SCHWAB_TOKEN_PATH", str(other / "token.json"))
    login(me, 0.01)                                                                # the own file is now the newest -- still ignored
    b = Broker(make_config(tmp_path), dry_run=True, log=logging.getLogger("t"))
    explicit = getattr(b, "data", b)
    explicit._get_client()
    assert built[-1] == str(other / "token.json")


def test_a_login_made_in_either_folder_later_is_picked_up_without_a_restart(tmp_path, monkeypatch):
    sb, me, other, built, login = _two_bots(tmp_path, monkeypatch, own_days_old=1, sibling_days_old=3)
    first = sb._get_client()
    assert built == [str(me / "token.json")] and sb._get_client() is first and len(built) == 1
    login(other, 0.001)                                                            # the user logged in from the sibling's folder
    second = sb._get_client()
    assert second is not first and built[-1] == str(other / "token.json") and sb.token_path == other / "token.json"
    login(me, 0.0001)                                                              # ... then from this bot's folder again
    third = sb._get_client()
    assert third is not second and built[-1] == str(me / "token.json")


def test_the_token_status_reports_the_login_that_is_actually_used(tmp_path, monkeypatch):
    sb, me, other, built, _ = _two_bots(tmp_path, monkeypatch, own_days_old=9, sibling_days_old=0.5)
    status = sb.token_status()
    assert status["state"] == "OK" and 0.4 < status["age_days"] < 0.6              # not the 9-day-old, dead own file


def test_a_switch_to_the_sibling_rebuilds_the_client_even_when_both_logins_are_the_same(tmp_path, monkeypatch):
    import shutil
    sb, me, other, built, login = _two_bots(tmp_path, monkeypatch, own_days_old=1, sibling_days_old=1)
    shutil.copyfile(me / "token.json", other / "token.json")                       # one login copied into both folders: same creation time
    first = sb._get_client()
    assert built[-1] == str(me / "token.json")
    (me / "token.json").unlink()                                                   # the own file disappears: the sibling's copy takes over
    second = sb._get_client()
    assert second is not first and built[-1] == str(other / "token.json")          # refreshes must be saved to the file in use


def test_a_sibling_token_file_without_a_creation_time_is_never_chosen(tmp_path, monkeypatch):
    sb, me, other, built, _ = _two_bots(tmp_path, monkeypatch, own_days_old=2, sibling_days_old=0.1)
    (other / "token.json").write_text("{}")                                        # no creation_timestamp: no way to know it is newer
    sb._get_client()
    assert built == [str(me / "token.json")]
    sb, me, other, built, _ = _two_bots(tmp_path / "again", monkeypatch, own_days_old=None, sibling_days_old=0.1)
    (other / "token.json").write_text("{}")
    with pytest.raises(BrokerError, match="token file not found"):
        sb._get_client()


def test_a_token_file_being_rewritten_by_another_bot_does_not_drop_the_client(tmp_path, monkeypatch, caplog):
    """2026-10-26: three bots on login A rewrite one token.json (not atomically). A reader that lands inside a write keeps the client it
    has -- it holds its own copy of the token -- instead of reporting a re-login that is not needed."""
    import json
    cfg = make_config(tmp_path)
    monkeypatch.setenv("SCHWAB_APP_KEY", "k")
    monkeypatch.setenv("SCHWAB_APP_SECRET", "s")
    b = Broker(cfg, dry_run=True, log=logging.getLogger("t"))
    sb = getattr(b, "data", b)
    token = tmp_path / "t.json"
    sb.token_path = token
    token.write_text(json.dumps({"creation_timestamp": 1_000_000.0, "token": {}}))
    built = []
    monkeypatch.setattr(broker_mod, "client_from_token_file", lambda *a, **k: built.append(a) or object())
    first = sb._get_client()
    monkeypatch.setattr(broker_mod.schwab_health, "token_created_at", lambda *a, **k: None)      # the file is half written right now
    with caplog.at_level(logging.WARNING):
        assert sb._get_client() is first and len(built) == 1
    assert not any("replaced" in r.message for r in caplog.records)
    monkeypatch.undo()
    monkeypatch.setenv("SCHWAB_APP_KEY", "k")
    monkeypatch.setenv("SCHWAB_APP_SECRET", "s")
    monkeypatch.setattr(broker_mod, "client_from_token_file", lambda *a, **k: built.append(a) or object())
    token.write_text(json.dumps({"creation_timestamp": 2_000_000.0, "token": {}}))              # a real new login still reloads
    assert sb._get_client() is not first and len(built) == 2
    token.unlink()                                                                              # a file that is GONE is not "being rewritten"
    with pytest.raises(BrokerError):
        sb._get_client()


def test_a_torn_pre_read_at_the_first_build_does_not_blind_the_bot_to_a_later_login(tmp_path, monkeypatch):
    import json
    cfg = make_config(tmp_path)
    monkeypatch.setenv("SCHWAB_APP_KEY", "k")
    monkeypatch.setenv("SCHWAB_APP_SECRET", "s")
    b = Broker(cfg, dry_run=True, log=logging.getLogger("t"))
    sb = getattr(b, "data", b)
    token = tmp_path / "t.json"
    sb.token_path = token
    token.write_text(json.dumps({"creation_timestamp": 1_000_000.0, "token": {}}))
    built = []
    monkeypatch.setattr(broker_mod, "client_from_token_file", lambda *a, **k: built.append(a) or object())
    real = broker_mod.schwab_health.token_created_at
    calls = {"n": 0}

    def torn_once(path, *a, **k):
        calls["n"] += 1
        return None if calls["n"] == 1 else real(path, *a, **k)                            # the first read lands inside a rewrite
    monkeypatch.setattr(broker_mod.schwab_health, "token_created_at", torn_once)
    first = sb._get_client()
    assert sb._token_created is not None and len(built) == 1
    token.write_text(json.dumps({"creation_timestamp": 2_000_000.0, "token": {}}))              # a real new login
    assert sb._get_client() is not first and len(built) == 2


def test_an_own_token_file_that_is_being_rewritten_is_kept_over_a_siblings_older_copy(tmp_path, monkeypatch):
    import json
    import time
    root = tmp_path / "schwab" / "Micah"
    me, sib = root / "me", root / "sib"
    for folder in (me, sib):
        folder.mkdir(parents=True)
        (folder / "schwab_login.py").write_text("")
    (sib / "token.json").write_text(json.dumps({"creation_timestamp": time.time() - 86400, "token": {}}))    # a day-old copy next door
    (me / "token.json").write_text("{\"creation_timestamp\": 17")                                           # mine, half written right now
    monkeypatch.setattr(broker_mod, "HERE", me)
    monkeypatch.delenv("SCHWAB_LIVE_ORDERS", raising=False)
    b = Broker(make_config(tmp_path), dry_run=True, log=logging.getLogger("t"))
    sb = getattr(b, "data", b)
    sb.token_path = me / "token.json"
    sb._explicit_token_path = False
    assert sb._select_token_path() == me / "token.json"


# ------------------------------------------------- 2026-10-30: bots on the SAME Schwab app key share the newest login across the person folders

def _person_layout(tmp_path, own_key="KEY-A"):
    """tmp/schwab/45DTE (this bot: the paper folder, no sibling next to it) + schwab/Micah/0dte (login A, same app key, newer token)
    + schwab/Ronnie/45DTE (login B, another app key, the newest token of all)."""
    import json
    import time
    root = tmp_path / "schwab"
    me = root / "45DTE"
    micah = root / "Micah" / "0dte"
    ronnie = root / "Ronnie" / "45DTE"
    now = time.time()
    for folder, key, age in ((me, own_key, 6 * 86400), (micah, "KEY-A", 2 * 86400), (ronnie, "KEY-B", 60)):
        folder.mkdir(parents=True)
        (folder / "schwab_login.py").write_text("")
        (folder / "token.json").write_text(json.dumps({"creation_timestamp": now - age, "token": {}}))
        if key:
            (folder / ".env").write_text(f"SCHWAB_APP_KEY={key}\n")
    return me, micah, ronnie


def test_the_paper_45dte_finds_micahs_login_by_the_shared_app_key_and_never_ronnies(tmp_path, monkeypatch):
    me, micah, ronnie = _person_layout(tmp_path)
    monkeypatch.setattr(broker_mod, "HERE", me)
    monkeypatch.delenv("SCHWAB_LIVE_ORDERS", raising=False)
    b = Broker(make_config(tmp_path, SCHWAB_APP_KEY="KEY-A"), dry_run=True)
    sb = getattr(b, "data", b)
    sb.token_path = me / "token.json"
    sb._explicit_token_path = False
    assert sb._sibling_token_paths() == [micah / "token.json"]                        # Ronnie's (another app) is not a sibling
    assert sb._select_token_path() == micah / "token.json"                            # the newer login A, not my stale copy, not login B


def test_without_a_known_app_key_only_the_folders_next_to_me_count(tmp_path, monkeypatch):
    me, micah, ronnie = _person_layout(tmp_path, own_key="")
    monkeypatch.setattr(broker_mod, "HERE", me)
    monkeypatch.delenv("SCHWAB_LIVE_ORDERS", raising=False)
    monkeypatch.delenv("SCHWAB_APP_KEY", raising=False)
    b = Broker(make_config(tmp_path), dry_run=True)
    sb = getattr(b, "data", b)
    sb.token_path = me / "token.json"
    sb._explicit_token_path = False
    assert sb._sibling_token_paths() == [] and sb._select_token_path() == me / "token.json"


def test_a_person_folder_bot_also_sees_the_paper_folders_login_when_it_is_the_newest(tmp_path, monkeypatch):
    import json
    import time
    me, micah, ronnie = _person_layout(tmp_path)
    (me / "token.json").write_text(json.dumps({"creation_timestamp": time.time(), "token": {}}))    # the owner just logged in from the paper folder
    monkeypatch.setattr(broker_mod, "HERE", micah)
    monkeypatch.delenv("SCHWAB_LIVE_ORDERS", raising=False)
    b = Broker(make_config(tmp_path, SCHWAB_APP_KEY="KEY-A"), dry_run=True)
    sb = getattr(b, "data", b)
    sb.token_path = micah / "token.json"
    sb._explicit_token_path = False
    assert sb._select_token_path() == me / "token.json"                                # same app, newest login: the valid one


def test_an_explicit_token_path_still_wins_over_the_shared_app_rule(tmp_path, monkeypatch):
    me, micah, ronnie = _person_layout(tmp_path)
    monkeypatch.setattr(broker_mod, "HERE", me)
    monkeypatch.delenv("SCHWAB_LIVE_ORDERS", raising=False)
    elsewhere = tmp_path / "elsewhere" / "token.json"                                 # a path of its own (pointing at ones own file = the default)
    b = Broker(make_config(tmp_path, SCHWAB_APP_KEY="KEY-A", SCHWAB_TOKEN_PATH=str(elsewhere)), dry_run=True)
    sb = getattr(b, "data", b)
    assert sb._explicit_token_path and sb._select_token_path() == elsewhere


# ---------------------------------------------------------------- the SPX $0.05 price grid (2026-11-03)
# Schwab rejected every closing order of the first live day: the limit was ask-side + $0.03, and SPX/SPXW spreads must be
# priced in $0.05 steps. Every SPX/SPXW price is now snapped DOWN to the grid before it is sent; stock options are untouched.

@pytest.mark.parametrize("price, snapped", [
    (0.83, 0.80), (0.85, 0.85), (0.88, 0.85), (0.84, 0.80), (0.849, 0.85), (1.00, 1.00), (1.83, 1.80), (3.07, 3.05), (0.04, 0.05), (0.0, 0.05),
])
def test_spx_prices_snap_down_to_the_five_cent_grid(price, snapped):
    assert broker_mod.snap_spx_price(price, "SPXW") == snapped
    assert broker_mod.snap_spx_price(price, "SPX") == snapped
    assert broker_mod.snap_spx_price(price, "spxw") == snapped


@pytest.mark.parametrize("price", [0.83, 0.88, 0.07, 1.23])
def test_stock_prices_are_not_snapped(price):
    assert broker_mod.snap_spx_price(price, "AAPL") == price
    assert broker_mod.snap_spx_price(price, None) == price


def test_the_next_close_price_always_climbs_a_full_tick_for_spx():
    assert broker_mod.next_close_price(0.80, 0.03, "SPXW") == 0.85      # 0.83 alone would snap back to 0.80 and never escalate
    assert broker_mod.next_close_price(0.80, 0.05, "SPXW") == 0.85
    assert broker_mod.next_close_price(0.80, 0.12, "SPXW") == 0.90
    assert broker_mod.next_close_price(0.73, 0.03, "AAPL") == 0.76


def test_an_spx_entry_is_sent_on_the_grid(brk):
    order = brk.place_credit_spread("SPX", EXP, "P", 7610, 7605, qty=1, limit_credit=0.88, root="SPXW")
    assert order["raw"]["price"] == "0.85" and order["limit_price"] == 0.85


def test_an_spx_close_is_sent_on_the_grid(brk):
    order = brk.place_close_spread("SPX", EXP, "P", 7610, 7605, qty=1, limit_debit=0.83, root="SPXW", time_in_force="day")
    assert order["raw"]["price"] == "0.80"


def test_a_stock_close_keeps_its_penny_price(brk):
    order = brk.place_close_spread("AAPL", date(2026, 11, 20), "P", 150, 145, qty=1, limit_debit=0.73, time_in_force="day")
    assert order["raw"]["price"] == "0.73"


def test_a_close_at_market_with_the_three_cent_slippage_lands_on_the_grid(tmp_path, fake, monkeypatch):
    monkeypatch.delenv("SCHWAB_LIVE_ORDERS", raising=False)
    b = Broker(make_config(tmp_path, CLOSE_SLIPPAGE=0.03), dry_run=True, log=logging.getLogger("test.broker"))
    b._client = fake
    b._sleep = lambda s: None
    order = b.close_spread_at_market("SPX", EXP, "P", 7610, 7605, qty=1, root="SPXW")
    assert order["raw"]["price"] == "1.80"           # ask-side 1.80 + 0.03 = 1.83: the 3 Nov orders went out like that and were rejected


def test_a_dry_run_re_price_of_an_spx_order_is_snapped(brk):
    order = brk.place_credit_spread("SPX", EXP, "P", 7610, 7605, qty=1, limit_credit=0.90, root="SPXW")
    assert brk.replace_order_price(order["id"], 0.83)["raw"]["price"] == "0.80"


def test_the_normalized_order_carries_schwabs_reason_text(brk):
    raw = dict(RAW_FILLED, status="REJECTED", statusDescription="Option order price rounded to nearest 0.05 (not a penny pilot)")
    order = brk._normalize_order(raw)
    assert order["status"] == "rejected"
    assert order["status_description"] == "Option order price rounded to nearest 0.05 (not a penny pilot)"
    assert brk._normalize_order(RAW_FILLED)["status_description"] is None


# ---------------------------------------------------------------- resting exits: the OCO (2026-11-06)

def _oco_child(order_id, status, order_type, price, filled=0.0, stop_price=None, fills=None, desc=None):
    raw = {"orderId": order_id, "status": status, "orderType": order_type, "price": price, "quantity": 1.0, "filledQuantity": filled,
           "duration": "DAY", "session": "NORMAL", "orderStrategyType": "SINGLE", "complexOrderStrategyType": "VERTICAL",
           "enteredTime": "2026-09-17T15:00:00+0000",
           "orderLegCollection": [
               {"legId": 1, "instruction": "BUY_TO_CLOSE", "quantity": 1.0,
                "instrument": {"assetType": "OPTION", "symbol": "SPXW  260917P07610000", "underlyingSymbol": "$SPX"}},
               {"legId": 2, "instruction": "SELL_TO_CLOSE", "quantity": 1.0,
                "instrument": {"assetType": "OPTION", "symbol": "SPXW  260917P07605000", "underlyingSymbol": "$SPX"}}]}
    if stop_price is not None:
        raw["stopPrice"] = stop_price
    if fills:
        raw["closeTime"] = "2026-09-17T15:20:00+0000"
        raw["orderActivityCollection"] = [{"activityType": "EXECUTION", "executionType": "FILL", "quantity": filled,
                                           "executionLegs": [{"legId": 1, "quantity": filled, "price": fills[0]},
                                                             {"legId": 2, "quantity": filled, "price": fills[1]}]}]
    if desc:
        raw["statusDescription"] = desc
    return raw


def _raw_oco(target_status="WORKING", stop_status="WORKING", target_fills=None, desc=None):
    return {"orderId": 900, "orderStrategyType": "OCO", "enteredTime": "2026-09-17T15:00:00+0000",
            "childOrderStrategies": [
                _oco_child(901, target_status, "NET_DEBIT", 0.6, filled=1.0 if target_fills else 0.0, fills=target_fills, desc=desc),
                _oco_child(902, stop_status, "STOP_LIMIT", 1.55, stop_price=1.4, desc=desc)]}


def test_an_oco_close_is_a_target_limit_and_a_stop_limit_on_the_grid(brk, fake):
    order = brk.place_oco_close("SPX", EXP, "P", 7610, 7605, qty=1, target_debit=0.63, stop_price=1.42, stop_limit=1.52,
                                root="SPXW", time_in_force="day")
    assert order["status"] == "dry_run" and order["order_class"] == "oco" and order["id"].startswith("DRY-")
    raw = order["raw"]
    assert raw["orderStrategyType"] == "OCO" and len(raw["childOrderStrategies"]) == 2
    target, stop = raw["childOrderStrategies"]
    assert target["orderType"] == "NET_DEBIT" and target["price"] == "0.60" and target["duration"] == "DAY"
    assert stop["orderType"] == "STOP_LIMIT" and stop["stopPrice"] == "1.40" and stop["price"] == "1.55" and "stopType" not in stop
    for child in (target, stop):
        assert child["orderStrategyType"] == "SINGLE" and child["complexOrderStrategyType"] == "VERTICAL" and child["session"] == "NORMAL"
        assert [l["instruction"] for l in child["orderLegCollection"]] == ["BUY_TO_CLOSE", "SELL_TO_CLOSE"]
        assert [l["instrument"]["symbol"] for l in child["orderLegCollection"]] == ["SPXW  260917P07610000", "SPXW  260917P07605000"]
        assert [l["quantity"] for l in child["orderLegCollection"]] == [1, 1]
    assert [c["exit_role"] for c in order["children"]] == ["target", "stop"]
    assert all(c["oco_parent_id"] == order["id"] for c in order["children"])
    assert brk.get_order(order["children"][1]["id"])["exit_role"] == "stop"          # the children can be read too
    assert brk.cancel_order(order["id"]) is True and brk.get_order(order["id"])["status"] == "canceled"
    assert fake.calls == []


def test_the_stops_limit_never_sits_at_or_below_the_stop(brk):
    order = brk.place_oco_close("SPX", EXP, "P", 7610, 7605, qty=1, target_debit=0.60, stop_price=1.40, stop_limit=1.40, root="SPXW")
    stop = order["raw"]["childOrderStrategies"][1]
    assert stop["stopPrice"] == "1.40" and stop["price"] == "1.45"


def test_a_stock_oco_keeps_penny_prices(brk):
    order = brk.place_oco_close("AAPL", date(2026, 11, 20), "P", 150, 145, qty=1, target_debit=0.63, stop_price=1.42, stop_limit=1.47)
    target, stop = order["raw"]["childOrderStrategies"]
    assert target["price"] == "0.63" and stop["stopPrice"] == "1.42" and stop["price"] == "1.47"


def test_a_configured_stop_type_is_sent(tmp_path, fake, monkeypatch):
    monkeypatch.delenv("SCHWAB_LIVE_ORDERS", raising=False)
    b = Broker(make_config(tmp_path, RESTING_STOP_TYPE="MARK"), dry_run=True, log=logging.getLogger("test.broker"))
    b._client = fake
    b._sleep = lambda s: None
    order = b.place_oco_close("SPX", EXP, "P", 7610, 7605, qty=1, target_debit=0.60, stop_price=1.40, stop_limit=1.55, root="SPXW")
    assert order["raw"]["childOrderStrategies"][1]["stopType"] == "MARK"


@pytest.mark.parametrize("price, up", [(1.52, 1.55), (1.55, 1.55), (1.56, 1.60), (0.01, 0.05), (3.07, 3.10), (0.80, 0.80)])
def test_spx_prices_snap_up_to_the_grid(price, up):
    assert broker_mod.snap_spx_price_up(price, "SPXW") == up
    assert broker_mod.snap_spx_price_up(price, "AAPL") == round(price, 2)


def test_an_oco_parent_is_normalized_with_its_children(brk):
    order = brk._normalize_order(_raw_oco("FILLED", "CANCELED", target_fills=(9.50, 8.90)))
    assert order["id"] == "900" and order["order_class"] == "oco" and order["status"] == "filled"
    assert order["filled_avg_price"] == pytest.approx(0.60) and order["filled_qty"] == 1
    assert [c["exit_role"] for c in order["children"]] == ["target", "stop"]
    assert [c["status"] for c in order["children"]] == ["filled", "canceled"]
    assert order["children"][1]["oco_parent_id"] == "900" and order["legs"] and order["symbol"] == "SPX"
    assert brk._normalize_order(_raw_oco())["status"] == "new"
    assert brk._normalize_order(_raw_oco("CANCELED", "CANCELED"))["status"] == "canceled"
    rej = brk._normalize_order(_raw_oco("REJECTED", "REJECTED", desc="Stop orders are not supported for this product"))
    assert rej["status"] == "rejected" and "not supported" in rej["status_description"]


def test_open_orders_list_an_ocos_children_with_their_parent(brk, fake):
    fake.orders_payload = [_raw_oco()]
    orders = brk.get_open_orders()
    assert [o["id"] for o in orders] == ["901", "902"] and all(o["oco_parent_id"] == "900" for o in orders)
    assert all(o["legs"] for o in orders) and [o["exit_role"] for o in orders] == ["target", "stop"]
    fake.orders_payload = [_raw_oco("FILLED", "CANCELED", target_fills=(9.50, 8.90))]
    assert brk.get_open_orders() == []


def test_a_half_rejected_oco_is_a_rejected_oco(brk):
    raw = _raw_oco("WORKING", "REJECTED", desc="Stop orders are not supported for this product")
    order = brk._normalize_order(raw)
    assert order["status"] == "rejected" and "not supported" in order["status_description"]
    assert [c["status"] for c in order["children"]] == ["new", "rejected"]
