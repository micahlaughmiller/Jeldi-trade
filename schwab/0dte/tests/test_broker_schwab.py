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
