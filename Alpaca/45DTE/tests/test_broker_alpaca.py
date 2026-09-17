"""Unit tests for the Alpaca Broker. No network: requests.Session is replaced by a fake."""

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import broker as br  # noqa: E402
from broker import Broker, BrokerError, occ_symbol, parse_occ, normalize_status  # noqa: E402

ET = ZoneInfo("US/Eastern")


class FakeResp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body) if not isinstance(body, str) else body
        self.content = self.text.encode()

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body


class FakeSession:
    def __init__(self):
        self.routes = {}
        self.calls = []

    def add(self, method, path_substr, status=200, body=None, once=False):
        self.routes.setdefault((method, path_substr), []).append((status, body, once))

    def request(self, method, url, headers=None, params=None, json=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": params, "json": json, "headers": headers})
        matches = [(len(sub), queue) for (m, sub), queue in self.routes.items() if m == method and sub in url and queue]
        if not matches:
            raise AssertionError(f"unexpected request {method} {url}")
        queue = max(matches, key=lambda x: x[0])[1]
        status, body, once = queue[0]
        if once:
            queue.pop(0)
        return FakeResp(status, body)


@pytest.fixture
def cfg():
    return SimpleNamespace(DRY_RUN=False, RISK_FREE_RATE=0.04, CLOSE_SLIPPAGE=0.05, CLOSE_RETRY_SEC=0, CLOSE_MAX_RETRIES=2)


@pytest.fixture
def b(cfg, monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    bk = Broker(cfg, dry_run=False, log=lambda m: None)
    bk._session = FakeSession()
    bk._sleep = lambda s: None
    return bk


def test_occ_symbol_build_and_parse():
    s = occ_symbol("SPXW", date(2026, 9, 17), "P", 7510)
    assert s == "SPXW260917P07510000"
    p = parse_occ(s)
    assert p == {"root": "SPXW", "expiration": date(2026, 9, 17), "right": "P", "strike": 7510.0}
    assert occ_symbol("AAPL", date(2026, 10, 16), "c", 232.5) == "AAPL261016C00232500"
    assert parse_occ("AAPL261016C00232500")["strike"] == 232.5
    with pytest.raises(BrokerError):
        parse_occ("NOTASYMBOL")
    assert Broker.occ_symbol("SPX", date(2026, 12, 18), "C", 6000) == "SPX261218C06000000"


def test_status_normalization():
    assert normalize_status("pending_new") == "pending"
    assert normalize_status("pending_cancel") == "pending"
    assert normalize_status("accepted") == "accepted"
    assert normalize_status("new") == "new"
    assert normalize_status("partially_filled") == "partially_filled"
    assert normalize_status("filled") == "filled"
    assert normalize_status("canceled") == "canceled"
    assert normalize_status("expired") == "expired"
    assert normalize_status("rejected") == "rejected"
    assert normalize_status("replaced") == "replaced"
    assert normalize_status("something_else") == "unknown"
    assert normalize_status(None) == "unknown"


def test_key_resolution_active_trader(cfg, monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    monkeypatch.setenv("ASTRA_API_KEY", "ak")
    monkeypatch.setenv("ASTRA_API_SECRET", "as")
    monkeypatch.setenv("CLAUDE_API_KEY", "ck")
    monkeypatch.setenv("CLAUDE_API_SECRET", "cs")
    monkeypatch.setenv("ACTIVE_TRADER", "CLAUDE")
    bk = Broker(cfg, log=lambda m: None)
    assert (bk.api_key, bk.secret_key, bk.trader) == ("ck", "cs", "CLAUDE")
    monkeypatch.setenv("ACTIVE_TRADER", "ASTRA")
    bk = Broker(cfg, log=lambda m: None)
    assert (bk.api_key, bk.secret_key) == ("ak", "as")
    assert bk.name == "alpaca" and bk.is_paper is True


def test_missing_keys_raise(monkeypatch):
    for k in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ASTRA_API_KEY", "ASTRA_API_SECRET", "ACTIVE_TRADER"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(BrokerError):
        Broker(SimpleNamespace(), log=lambda m: None)


def test_get_account(b):
    b._session.add("GET", "/v2/account", body={"equity": "10123.45", "cash": "9000", "buying_power": "20000", "options_buying_power": "9000", "id": "abc"})
    a = b.get_account()
    assert a == {"equity": 10123.45, "cash": 9000.0, "buying_power": 20000.0, "options_buying_power": 9000.0, "account_id": "abc"}
    assert b._session.calls[0]["headers"]["APCA-API-KEY-ID"] == "k"


def test_http_error_and_retry(b):
    b._session.add("GET", "/v2/account", status=500, body="boom", once=True)
    b._session.add("GET", "/v2/account", status=200, body={"equity": "1", "cash": "1", "buying_power": "1", "options_buying_power": "1", "id": "x"})
    assert b.get_account()["equity"] == 1.0
    b2 = b
    b2._session = FakeSession()
    b2._session.add("GET", "/v2/positions", status=403, body={"message": "forbidden"})
    with pytest.raises(BrokerError) as ei:
        b2.get_positions()
    assert str(ei.value).startswith("403")


def test_positions_normalization(b):
    b._session.add("GET", "/v2/positions", body=[
        {"symbol": "SPXW260917P07510000", "qty": "-1", "avg_entry_price": "1.25", "current_price": "0.9", "market_value": "-90", "unrealized_pl": "35", "asset_class": "us_option"},
        {"symbol": "AAPL", "qty": "10", "avg_entry_price": "200", "current_price": "210", "market_value": "2100", "unrealized_pl": "100", "asset_class": "us_equity"},
    ])
    pos = b.get_positions()
    assert pos[0]["qty"] == -1 and pos[0]["underlying"] == "SPX" and pos[0]["asset_class"] == "option"
    assert pos[0]["expiration"] == date(2026, 9, 17) and pos[0]["strike"] == 7510.0 and pos[0]["right"] == "P"
    assert pos[1]["asset_class"] == "stock" and pos[1]["expiration"] is None and pos[1]["qty"] == 10


MLEG_RAW = {
    "id": "o1", "status": "filled", "order_class": "mleg", "qty": "2", "filled_qty": "2", "limit_price": "1.50",
    "filled_avg_price": None, "time_in_force": "day", "symbol": None, "side": None,
    "submitted_at": "2026-09-17T13:45:00.123456789Z", "filled_at": "2026-09-17T13:45:01Z",
    "legs": [
        {"symbol": "SPXW260917P06300000", "side": "sell", "position_intent": "sell_to_open", "ratio_qty": "1", "status": "filled", "filled_avg_price": "2.10", "filled_qty": "2"},
        {"symbol": "SPXW260917P06295000", "side": "buy", "position_intent": "buy_to_open", "ratio_qty": "1", "status": "filled", "filled_avg_price": "0.55", "filled_qty": "2"},
    ],
}


def test_normalize_mleg_order(b):
    o = b._normalize_order(MLEG_RAW)
    assert o["order_class"] == "mleg" and o["status"] == "filled"
    assert o["qty"] == 2 and o["filled_qty"] == 2 and o["limit_price"] == 1.5
    assert o["filled_avg_price"] == pytest.approx(1.55)
    assert o["symbol"] == "SPX"
    assert o["legs"][0]["ratio_qty"] == 1 and o["legs"][0]["status"] == "filled" and o["legs"][1]["filled_avg_price"] == 0.55
    assert o["submitted_at"].tzinfo is not None and o["submitted_at"].astimezone(ET).hour == 9
    assert o["filled_at"] is not None and o["raw"] is MLEG_RAW


def test_normalize_mleg_debit_close_is_positive(b):
    raw = dict(MLEG_RAW)
    raw["legs"] = [
        {"symbol": "SPXW260917P06300000", "side": "buy", "position_intent": "buy_to_close", "ratio_qty": "1", "status": "filled", "filled_avg_price": "2.10"},
        {"symbol": "SPXW260917P06295000", "side": "sell", "position_intent": "sell_to_close", "ratio_qty": "1", "status": "filled", "filled_avg_price": "0.55"},
    ]
    assert b._normalize_order(raw)["filled_avg_price"] == pytest.approx(1.55)


def test_normalize_mleg_unfilled_uses_top_level_or_none(b):
    raw = dict(MLEG_RAW, status="pending_new", filled_qty="0")
    raw["legs"] = [dict(l, status="pending_new", filled_avg_price=None) for l in MLEG_RAW["legs"]]
    o = b._normalize_order(raw)
    assert o["status"] == "pending" and o["filled_avg_price"] is None
    raw2 = dict(raw, filled_avg_price="-1.40")
    assert b._normalize_order(raw2)["filled_avg_price"] == 1.4


def test_normalize_simple_order(b):
    raw = {"id": "s1", "status": "partially_filled", "symbol": "AAPL", "side": "buy", "qty": "10", "filled_qty": "4", "limit_price": "200.5",
           "filled_avg_price": "200.25", "time_in_force": "gtc", "submitted_at": "2026-09-17T14:00:00Z", "filled_at": None, "order_class": "simple"}
    o = b._normalize_order(raw)
    assert o["order_class"] == "simple" and o["status"] == "partially_filled" and o["legs"] == []
    assert o["filled_avg_price"] == 200.25 and o["side"] == "buy" and o["filled_at"] is None


def test_get_order_404_returns_none(b):
    b._session.add("GET", "/v2/orders/missing", status=404, body={"message": "order not found"})
    assert b.get_order("missing") is None


def test_wait_for_fill_polls_until_terminal(b):
    b._session.add("GET", "/v2/orders/o1", body=dict(MLEG_RAW, status="new"), once=True)
    b._session.add("GET", "/v2/orders/o1", body=MLEG_RAW)
    o = b.wait_for_fill("o1", 10, poll_sec=0.01)
    assert o["status"] == "filled"
    assert len([c for c in b._session.calls if "/v2/orders/o1" in c["url"]]) == 2


def test_chain_parsing_with_delta(b):
    exp = date.today() + timedelta(days=30)
    yymmdd = exp.strftime("%y%m%d")
    spot = 100.0
    snaps = {
        f"XYZ{yymmdd}P00100000": {"latestQuote": {"ap": 3.10, "bp": 3.00, "t": "2026-09-17T13:40:00Z"}, "latestTrade": {"p": 3.05}, "greeks": None, "impliedVolatility": None},
        f"XYZ{yymmdd}P00090000": {"latestQuote": {"ap": 0.60, "bp": 0.50, "t": "2026-09-17T13:40:00Z"}, "latestTrade": None, "greeks": None, "impliedVolatility": None},
        f"XYZ{yymmdd}P00080000": {"latestQuote": {"ap": 0.0, "bp": 0.0}, "greeks": None},
        f"XYZ{yymmdd}C00100000": {"latestQuote": {"ap": 3.0, "bp": 2.9}, "greeks": None},
    }
    b._session.add("GET", "/v1beta1/options/snapshots/XYZ", body={"snapshots": {k: v for k, v in list(snaps.items())[:2]}, "next_page_token": "tok"}, once=True)
    b._session.add("GET", "/v1beta1/options/snapshots/XYZ", body={"snapshots": {k: v for k, v in list(snaps.items())[2:]}, "next_page_token": None})
    rows = b.get_option_chain("XYZ", exp, "P", strike_min=70, strike_max=110, spot=spot)
    assert [r["strike"] for r in rows] == [90.0, 100.0]
    atm = rows[1]
    assert atm["bid"] == 3.0 and atm["ask"] == 3.1 and atm["mid"] == pytest.approx(3.05) and atm["last"] == 3.05
    assert atm["delta"] is not None and -0.6 < atm["delta"] < -0.4
    assert atm["iv"] is not None and 0.05 < atm["iv"] < 1.0
    assert rows[0]["delta"] is not None and -0.3 < rows[0]["delta"] < 0
    assert rows[0]["last"] is None and atm["quote_time"].tzinfo is not None
    assert atm["root"] == "XYZ" and atm["underlying"] == "XYZ" and atm["right"] == "P" and atm["expiration"] == exp
    first = b._session.calls[0]["params"]
    assert first["feed"] == "indicative" and first["type"] == "put" and first["expiration_date"] == exp.isoformat()
    assert first["strike_price_gte"] == "70.00" and first["strike_price_lte"] == "110.00"
    assert b._session.calls[1]["params"]["page_token"] == "tok"


def test_chain_without_spot_has_no_greeks(b):
    exp = date.today() + timedelta(days=5)
    occ = f"SPXW{exp.strftime('%y%m%d')}P06500000"
    b._session.add("GET", "/v1beta1/options/snapshots/SPXW", body={"snapshots": {occ: {"latestQuote": {"ap": 1.0, "bp": 0.9}}}})
    rows = b.get_option_chain("SPXW", exp, "P")
    assert len(rows) == 1 and rows[0]["delta"] is None and rows[0]["iv"] is None
    assert rows[0]["underlying"] == "SPX" and rows[0]["root"] == "SPXW"


def test_expirations_with_roots_and_pagination(b):
    d1, d2 = (date.today() + timedelta(days=1)).isoformat(), (date.today() + timedelta(days=15)).isoformat()
    b._session.add("GET", "/v2/options/contracts", body={"option_contracts": [
        {"symbol": "a", "expiration_date": d1, "root_symbol": "SPXW"}], "next_page_token": "p2"}, once=True)
    b._session.add("GET", "/v2/options/contracts", body={"option_contracts": [
        {"symbol": "b", "expiration_date": d2, "root_symbol": "SPX"}, {"symbol": "c", "expiration_date": d2, "root_symbol": "SPXW"}], "next_page_token": None})
    exps = b.get_expirations("SPX", 0, 30)
    assert exps == [date.fromisoformat(d1), date.fromisoformat(d2)]
    roots = b.get_expiration_roots("SPX", 0, 30)
    assert roots[date.fromisoformat(d2)] == {"SPX", "SPXW"} and roots[date.fromisoformat(d1)] == {"SPXW"}
    assert b._session.calls[0]["params"]["underlying_symbols"] == "SPX"
    assert b._session.calls[1]["params"]["page_token"] == "p2"
    assert len(b._session.calls) == 2


def test_place_credit_spread_payload(b):
    b._session.add("POST", "/v2/orders", body=dict(MLEG_RAW, status="pending_new"))
    o = b.place_credit_spread("SPX", date(2026, 9, 17), "P", 6300, 6295, 1, 1.5, time_in_force="day", root="SPXW", client_tag="tag1")
    sent = b._session.calls[0]["json"]
    assert sent["order_class"] == "mleg" and sent["qty"] == "1" and sent["type"] == "limit" and sent["limit_price"] == "1.50" and sent["time_in_force"] == "day"
    assert sent["client_order_id"] == "tag1"
    assert sent["legs"] == [
        {"symbol": "SPXW260917P06300000", "ratio_qty": "1", "side": "sell", "position_intent": "sell_to_open"},
        {"symbol": "SPXW260917P06295000", "ratio_qty": "1", "side": "buy", "position_intent": "buy_to_open"},
    ]
    assert o["status"] == "pending" and o["id"] == "o1"


def test_place_close_spread_payload(b):
    b._session.add("POST", "/v2/orders", body=MLEG_RAW)
    b.place_close_spread("AAPL", date(2026, 10, 16), "C", 240, 245, 2, 0.35)
    sent = b._session.calls[0]["json"]
    assert sent["qty"] == "2" and sent["limit_price"] == "0.35" and sent["time_in_force"] == "gtc"
    assert sent["legs"][0] == {"symbol": "AAPL261016C00240000", "ratio_qty": "1", "side": "buy", "position_intent": "buy_to_close"}
    assert sent["legs"][1] == {"symbol": "AAPL261016C00245000", "ratio_qty": "1", "side": "sell", "position_intent": "sell_to_close"}


def test_dry_run_behaviour(cfg, monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    logs = []
    bk = Broker(cfg, dry_run=True, log=logs.append)
    bk._session = FakeSession()
    bk._sleep = lambda s: None
    o = bk.place_credit_spread("SPX", date(2026, 9, 17), "P", 6300, 6295, 1, 1.5, root="SPXW")
    assert o["id"].startswith("DRY-") and o["status"] == "dry_run" and o["order_class"] == "mleg"
    assert o["legs"][0]["symbol"] == "SPXW260917P06300000" and o["limit_price"] == 1.5 and o["qty"] == 1
    assert any("DRY RUN" in m and "SPXW260917P06300000" in m for m in logs)
    assert bk.get_order(o["id"]) is o
    assert bk.wait_for_fill(o["id"], 5)["status"] == "dry_run"
    bk._session.add("GET", "/v1beta1/options/snapshots/SPXW", body={"snapshots": {
        "SPXW260917P06300000": {"latestQuote": {"ap": 1.2, "bp": 1.0}}, "SPXW260917P06295000": {"latestQuote": {"ap": 0.4, "bp": 0.3}}}})
    c = bk.close_spread_at_market("SPX", date(2026, 9, 17), "P", 6300, 6295, 1, root="SPXW")
    assert c["status"] == "dry_run" and c["limit_price"] == 0.95 and c["legs"][0]["position_intent"] == "buy_to_close"
    r = bk.replace_order_price(o["id"], 1.4)
    assert r["status"] == "dry_run" and r["id"].startswith("DRY-")
    assert bk.cancel_order(o["id"]) is True and bk.get_order(o["id"])["status"] == "canceled"
    assert isinstance(bk.cancel_all_orders(), int)
    assert all(c["method"] == "GET" for c in bk._session.calls)
    dflt = Broker(SimpleNamespace(DRY_RUN=True), log=lambda m: None)
    assert dflt.dry_run is True
    assert Broker(SimpleNamespace(), log=lambda m: None).dry_run is False


def test_cancel_order_and_cancel_all(b):
    b._session.add("DELETE", "/v2/orders/o1", status=204, body="")
    b._session.add("DELETE", "/v2/orders/gone", status=422, body={"message": "order is not cancelable"})
    assert b.cancel_order("o1") is True
    assert b.cancel_order("gone") is False
    b._session.add("DELETE", "/v2/orders", status=207, body=[{"id": "a", "status": 200}, {"id": "b", "status": 200}])
    assert b.cancel_all_orders() == 2


def test_replace_order_price_simple_patch(b):
    simple = {"id": "s1", "status": "new", "symbol": "AAPL", "side": "buy", "qty": "1", "filled_qty": "0", "limit_price": "200", "time_in_force": "gtc", "order_class": "simple", "submitted_at": "2026-09-17T14:00:00Z"}
    b._session.add("GET", "/v2/orders/s1", body=simple)
    b._session.add("PATCH", "/v2/orders/s1", body=dict(simple, id="s2", limit_price="199", status="pending_replace"))
    o = b.replace_order_price("s1", 199)
    assert o["id"] == "s2" and o["limit_price"] == 199.0
    assert b._session.calls[-1]["json"] == {"limit_price": "199.00"}


def test_replace_order_price_mleg_falls_back_to_cancel_resubmit(b):
    working = dict(MLEG_RAW, status="new", filled_qty="0")
    working["legs"] = [dict(l, status="new", filled_avg_price=None) for l in MLEG_RAW["legs"]]
    b._session.add("GET", "/v2/orders/o1", body=working, once=True)
    b._session.add("PATCH", "/v2/orders/o1", status=422, body={"message": "mleg not supported"})
    b._session.add("DELETE", "/v2/orders/o1", status=204, body="")
    b._session.add("GET", "/v2/orders/o1", body=dict(working, status="canceled"))
    b._session.add("POST", "/v2/orders", body=dict(working, id="o2", limit_price="1.40"))
    o = b.replace_order_price("o1", 1.4)
    assert o["id"] == "o2"
    post = [c for c in b._session.calls if c["method"] == "POST"][0]["json"]
    assert post["limit_price"] == "1.40" and post["qty"] == "2" and post["time_in_force"] == "day"
    assert post["legs"][0]["position_intent"] == "sell_to_open" and post["legs"][1]["side"] == "buy"


def test_close_spread_at_market_escalates(b):
    exp = date(2026, 9, 17)
    s_occ, l_occ = "SPXW260917P06300000", "SPXW260917P06295000"
    snaps = {s_occ: {"latestQuote": {"ap": 1.20, "bp": 1.00}}, l_occ: {"latestQuote": {"ap": 0.40, "bp": 0.30}}}
    b._session.add("GET", "/v1beta1/options/snapshots/SPXW", body={"snapshots": snaps})
    working = dict(MLEG_RAW, status="new", filled_qty="0", id="c1")
    working["legs"] = [dict(l, status="new", filled_avg_price=None) for l in MLEG_RAW["legs"]]
    b._session.add("POST", "/v2/orders", body=working, once=True)
    b._session.add("GET", "/v2/orders/c1", body=working, once=True)
    b._session.add("DELETE", "/v2/orders/c1", status=204, body="")
    b._session.add("GET", "/v2/orders/c1", body=dict(working, status="canceled"))
    filled = dict(MLEG_RAW, id="c2", qty="1", filled_qty="1")
    b._session.add("POST", "/v2/orders", body=filled)
    b._session.add("GET", "/v2/orders/c2", body=filled)
    o = b.close_spread_at_market("SPX", exp, "P", 6300, 6295, 1, root="SPXW")
    assert o["status"] == "filled" and o["id"] == "c2"
    posts = [c["json"] for c in b._session.calls if c["method"] == "POST"]
    assert posts[0]["limit_price"] == "0.95"
    assert posts[1]["limit_price"] == "1.00"
    assert posts[0]["legs"][0]["position_intent"] == "buy_to_close"


def test_close_spread_at_market_exhausts_and_raises(b):
    snaps = {"SPXW260917P06300000": {"latestQuote": {"ap": 1.2, "bp": 1.0}}, "SPXW260917P06295000": {"latestQuote": {"ap": 0.4, "bp": 0.3}}}
    b._session.add("GET", "/v1beta1/options/snapshots/SPXW", body={"snapshots": snaps})
    working = dict(MLEG_RAW, status="new", filled_qty="0", id="c1")
    working["legs"] = [dict(l, status="new", filled_avg_price=None) for l in MLEG_RAW["legs"]]
    b._session.add("POST", "/v2/orders", body=working)
    b._session.add("GET", "/v2/orders/c1", body=dict(working, status="canceled"))
    b._session.add("DELETE", "/v2/orders/c1", status=204, body="")
    with pytest.raises(BrokerError):
        b.close_spread_at_market("SPX", date(2026, 9, 17), "P", 6300, 6295, 1, root="SPXW")
    assert len([c for c in b._session.calls if c["method"] == "POST"]) == 3


def test_pnl_summary_skips_zero_equity(b, monkeypatch):
    now = datetime.now(ET)
    jan1 = datetime(now.year, 1, 1, 16, tzinfo=ET)
    m1 = datetime(now.year, now.month, 1, 16, tzinfo=ET)
    yest = now - timedelta(days=1)
    ts = [int((jan1 - timedelta(days=10)).timestamp()), int(jan1.timestamp()), int((jan1 + timedelta(days=3)).timestamp()), int((m1 - timedelta(days=1)).timestamp()), int(yest.timestamp())]
    eq = [0.0, 0.0, 10000.0, 10200.0, 10300.0]
    b._session.add("GET", "/v2/account", body={"equity": "10400", "cash": "1", "buying_power": "1", "options_buying_power": "1", "id": "x"})
    b._session.add("GET", "/v2/account/portfolio/history", body={"timestamp": ts, "equity": eq})
    p = b.get_pnl_summary()
    assert p["today"] == pytest.approx(100.0)
    assert p["ytd"] == pytest.approx(400.0)
    assert p["mtd"] is not None
    assert p["as_of"].tzinfo is not None


def test_pnl_summary_never_raises(b):
    b._session.add("GET", "/v2/account", status=500, body="down")
    p = b.get_pnl_summary()
    assert p["ytd"] is None and p["mtd"] is None and p["today"] is None


def test_get_spot_stock_midpoint(b):
    b._session.add("GET", "/v2/stocks/AAPL/quotes/latest", body={"symbol": "AAPL", "quote": {"ap": 201.0, "bp": 200.0}})
    assert b.get_spot("AAPL") == 200.5
    assert b._session.calls[0]["params"]["feed"] == "iex"


def test_get_spot_index_uses_yfinance(b, monkeypatch):
    monkeypatch.setattr(Broker, "_yf_spot", staticmethod(lambda t: 6500.5 if t == "^GSPC" else 0))
    assert b.get_spot("SPX") == 6500.5
    assert b.get_spot("^GSPC") == 6500.5
    assert b._session.calls == []


def test_record_daily_equity(b, tmp_path):
    b.log_dir = tmp_path
    b._session.add("GET", "/v2/account", body={"equity": "10500", "cash": "1", "buying_power": "1", "options_buying_power": "1", "id": "x"})
    b.record_daily_equity()
    b.record_daily_equity()
    lines = (tmp_path / "equity_history.csv").read_text().strip().splitlines()
    assert lines[0] == "date,equity" and len(lines) == 2 and lines[1].endswith("10500.00")
