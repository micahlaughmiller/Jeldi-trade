"""Schwab 0DTE go-live safety (2026-10-15): the selected account, the capital cap and the contract cap.

Schwab has no paper trading, so the live model starts small: orders go only to the account the owner selected after the last login, the bot
works with the greater of $1,000 or 25% of that account (never more than it holds), and an opening order is at most 1 contract. The simulator
is never touched. No network and no real order: every client here is a fake."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

import broker as broker_mod
import risk_manager
import schwab_accounts
from broker import Broker, BrokerError
from test_broker_schwab import EXP, FakeClient, _resp, make_config

FOLDER = Path(__file__).resolve().parents[1]
LIVE_KEYS = dict(LIVE_CAPITAL_CAP_ENABLED=True, LIVE_CAPITAL_FLOOR_USD=1000.0, LIVE_CAPITAL_PCT=0.25, LIVE_MAX_CONTRACTS_PER_TRADE=1)


class LiveFake(FakeClient):
    """A client that records the orders a LIVE broker sends (and sends them nowhere)."""

    def __init__(self):
        super().__init__()
        self.placed: list[tuple[str, dict]] = []

    def place_order(self, account_hash, order_json):
        self.placed.append((account_hash, order_json))
        return _resp(201, None, {"Location": "https://api.schwabapi.com/trader/v1/accounts/x/orders/777"})


def set_equity(fake: FakeClient, value: float | None, cash: float = 12000.0, buying_power: float = 50000.0, obp: float = 24000.0) -> None:
    bal = fake.account_payload["securitiesAccount"]["currentBalances"]
    bal.update(cashBalance=cash, buyingPower=buying_power, buyingPowerNonMarginableTrade=obp)
    for key in ("liquidationValue", "equity"):
        if value is None:
            bal.pop(key, None)
        else:
            bal[key] = value


def select(tmp_path: Path, hash_value="HASH1", last4="4321", kind="MARGIN") -> Path:
    path = tmp_path / "selected_account.json"
    schwab_accounts.write_selection(path, {"hash_value": hash_value, "last4": last4, "type": kind, "selected_at": "2026-10-15T08:00:00"})
    return path


def make(tmp_path, monkeypatch, fake=None, live=False, **overrides):
    """A dry-run broker (default) or a LIVE one (live=True: SCHWAB_LIVE_ORDERS=true) wired to a fake client."""
    monkeypatch.setenv("SCHWAB_LIVE_ORDERS", "true" if live else "false")
    cfg = make_config(tmp_path, SCHWAB_MODE="live" if live else "dry_run", **{**LIVE_KEYS, **overrides})
    b = Broker(cfg, dry_run=not live, log=logging.getLogger("test.golive"))
    assert b.dry_run is (not live)
    b._client = fake or (LiveFake() if live else FakeClient())
    b._sleep = lambda s: None
    return b


# ---------------------------------------------------------------- the account selection

def test_live_orders_are_refused_until_an_account_is_selected(tmp_path, monkeypatch, caplog):
    fake = LiveFake()
    b = make(tmp_path, monkeypatch, fake, live=True)
    with caplog.at_level(logging.WARNING), pytest.raises(BrokerError, match="No Schwab account has been selected"):
        b.place_credit_spread("SPX", EXP, "P", 7610, 7605, qty=1, limit_credit=1.5, root="SPXW")
    assert fake.placed == []
    assert not any("[LIVE]" in r.message and "sending" in r.message for r in caplog.records)        # it never claimed to be sending


def test_live_orders_go_to_the_selected_account_whatever_the_index_says(tmp_path, monkeypatch):
    fake = LiveFake()
    b = make(tmp_path, monkeypatch, fake, live=True, SCHWAB_ACCOUNT_INDEX=0)
    select(tmp_path, "HASH1", "4321")
    b.place_credit_spread("SPX", EXP, "P", 7610, 7605, qty=1, limit_credit=1.5, root="SPXW")
    assert [h for h, _ in fake.placed] == ["HASH1"]                  # index 0 would have been HASH0


@pytest.mark.parametrize("hash_value,last4", [("GONE", "4321"), ("HASH1", "9999")])
def test_a_selection_that_is_no_longer_linked_stops_live_orders(tmp_path, monkeypatch, hash_value, last4):
    fake = LiveFake()
    b = make(tmp_path, monkeypatch, fake, live=True)
    select(tmp_path, hash_value, last4)
    with pytest.raises(BrokerError, match="not linked to this login any more"):
        b.place_credit_spread("SPX", EXP, "P", 7610, 7605, qty=1, limit_credit=1.5, root="SPXW")
    assert fake.placed == []


def test_choosing_another_account_while_the_bot_runs_stops_new_orders_but_everything_else_stays_on_the_account_in_use(tmp_path, monkeypatch):
    fake = LiveFake()
    b = make(tmp_path, monkeypatch, fake, live=True)
    path = select(tmp_path, "HASH0", "5678")
    b.place_credit_spread("SPX", EXP, "P", 7610, 7605, qty=1, limit_credit=1.5, root="SPXW")        # the bot resolves its account: HASH0
    schwab_accounts.write_selection(path, {"hash_value": "HASH1", "last4": "4321", "type": "MARGIN", "selected_at": "x"})
    os.utime(path, ns=(10**18, 10**18))                              # a distinct modification time, whatever the file system's resolution
    with pytest.raises(BrokerError, match="NO new orders until it is restarted"):
        b.place_credit_spread("SPX", EXP, "P", 7610, 7605, qty=1, limit_credit=1.5, root="SPXW")
    b.place_close_spread("SPX", EXP, "P", 7610, 7605, qty=1, limit_debit=0.5, root="SPXW")          # a close still goes out, to the account holding the position
    b.get_account()
    b.get_open_orders()
    assert [h for h, _ in fake.placed] == ["HASH0", "HASH0"]
    assert {c[1][0] for c in fake.calls if c[0] in ("get_account", "get_orders_for_account")} == {"HASH0"}
    schwab_accounts.write_selection(path, {"hash_value": "HASH0", "last4": "5678", "type": "MARGIN", "selected_at": "y"})     # the same account saved again
    os.utime(path, ns=(2 * 10**18, 2 * 10**18))
    b.place_credit_spread("SPX", EXP, "P", 7610, 7605, qty=1, limit_credit=1.5, root="SPXW")
    path.unlink()                                                    # the choice removed while running
    with pytest.raises(BrokerError, match="removed or is unreadable"):
        b.place_credit_spread("SPX", EXP, "P", 7610, 7605, qty=1, limit_credit=1.5, root="SPXW")
    b.place_close_spread("SPX", EXP, "P", 7610, 7605, qty=1, limit_debit=0.5, root="SPXW")
    assert [h for h, _ in fake.placed] == ["HASH0"] * 4
    select(tmp_path, "HASH1", "4321")                                # a restart picks the new choice up
    fake2 = LiveFake()
    restarted = make(tmp_path, monkeypatch, fake2, live=True)
    restarted.place_credit_spread("SPX", EXP, "P", 7610, 7605, qty=1, limit_credit=1.5, root="SPXW")
    assert [h for h, _ in fake2.placed] == ["HASH1"]


def test_a_dry_run_bot_without_a_selection_still_uses_the_index_and_says_so(tmp_path, monkeypatch):
    b = make(tmp_path, monkeypatch, SCHWAB_ACCOUNT_INDEX=1)
    assert b._hash() == "HASH1"
    line = b.account_report_lines()[0]
    assert "...4321" in line and "NO account selected yet" in line and "DRY-RUN" in line and "REAL MONEY" not in line


def test_the_simulator_never_reads_the_selection(tmp_path, monkeypatch):
    monkeypatch.delenv("SCHWAB_LIVE_ORDERS", raising=False)
    fake = FakeClient()
    cfg = make_config(tmp_path, SCHWAB_MODE="sim", **LIVE_KEYS)
    b = broker_mod.SchwabBroker(cfg, dry_run=True)                   # the data broker the simulator wraps
    b._client = fake
    select(tmp_path, "GONE", "0000")                                 # a stale choice must not break (or redirect) the simulator
    assert b._hash() == "HASH0"
    acct = b.get_account()
    assert acct["equity"] == 25000.5 and "raw_equity" not in acct   # no cap in the simulator
    assert b.account_report_lines() == [] and b._contract_cap() is None


def test_the_start_report_names_the_account_and_says_live_orders_are_real_money(tmp_path, monkeypatch):
    b = make(tmp_path, monkeypatch, live=True)
    select(tmp_path, "HASH1", "4321", "MARGIN")
    b.get_account()
    lines = b.account_report_lines()
    assert lines[0].startswith("Account: ...4321 (MARGIN) [selected]") and "LIVE ORDERS: REAL MONEY" in lines[0]
    assert any(l.startswith("Capital: $") and "usable of" in l for l in lines)
    assert any("at most 1 per opening order" in l for l in lines)
    unselected = make(tmp_path / "other", monkeypatch, live=True)
    assert unselected.account_report_lines()[0].startswith("Account: NOT SELECTED")


# ---------------------------------------------------------------- the capital cap

def test_the_bot_sees_the_greater_of_1000_or_25_percent_of_the_account(tmp_path, monkeypatch):
    b = make(tmp_path, monkeypatch)
    acct = b.get_account()                                           # total value 25,000.50 -> 25% = 6,250.125
    assert acct["equity"] == 6250.12 and acct["capital_cap"] == 6250.12
    assert acct["cash"] == 6250.12 and acct["buying_power"] == 6250.12 and acct["options_buying_power"] == 6250.12     # each real figure is larger
    assert acct["raw_equity"] == 25000.5 and acct["raw_cash"] == 12000.0 and acct["raw_buying_power"] == 50000.0
    assert acct["account_id"] == "12345678"


@pytest.mark.parametrize("value,expected", [
    (3_000.0, 1_000.0),        # 25% would be 750: the $1,000 floor wins
    (4_000.0, 1_000.0),        # exactly equal
    (4_400.0, 1_100.0),        # just above: 25% wins
    (800.0, 800.0),            # never more than the account actually holds
    (100_000.0, 25_000.0),
])
def test_cap_values(tmp_path, monkeypatch, value, expected):
    fake = FakeClient()
    set_equity(fake, value, cash=value, buying_power=value * 2, obp=value * 2)
    acct = make(tmp_path, monkeypatch, fake).get_account()
    assert acct["capital_cap"] == expected and acct["equity"] == expected
    assert acct["buying_power"] <= expected and acct["options_buying_power"] <= expected and acct["cash"] <= expected


def test_small_real_balances_are_not_inflated_to_the_cap(tmp_path, monkeypatch):
    fake = FakeClient()
    set_equity(fake, 5_000.0, cash=300.0, buying_power=450.0, obp=200.0)
    acct = make(tmp_path, monkeypatch, fake).get_account()
    assert acct["equity"] == 1250.0
    assert (acct["cash"], acct["buying_power"], acct["options_buying_power"]) == (300.0, 450.0, 200.0)      # min(real, cap)


def test_a_loss_lowers_the_virtual_account_but_a_profit_or_a_deposit_never_raises_it_above_the_cap(tmp_path, monkeypatch):
    fake = FakeClient()
    set_equity(fake, 10_000.0)
    b = make(tmp_path, monkeypatch, fake)
    assert b.get_account()["equity"] == 2500.0
    set_equity(fake, 9_850.0)                                        # the account lost $150 since the first read
    assert b.get_account()["equity"] == 2350.0
    set_equity(fake, 10_200.0)                                       # a profit: still the cap, never above it
    assert b.get_account()["equity"] == 2500.0
    set_equity(fake, 60_000.0)                                       # a deposit (or the owner's hand trades): the same
    assert b.get_account()["equity"] == 2500.0
    set_equity(fake, 100.0)                                          # a total loss never goes below zero
    assert b.get_account()["equity"] == 0.0


def test_a_restart_the_same_day_keeps_the_same_base_and_a_new_day_starts_a_new_one(tmp_path, monkeypatch):
    fake = FakeClient()
    set_equity(fake, 10_000.0)
    make(tmp_path, monkeypatch, fake).get_account()                  # first read of the day: base 10,000 -> cap 2,500
    saved = json.loads((tmp_path / "logs" / "live_capital.json").read_text())
    assert saved["raw_start"] == 10_000.0 and saved["cap"] == 2500.0 and saved["date"] == datetime.now(broker_mod.ET).date().isoformat()
    set_equity(fake, 9_900.0)
    restarted = make(tmp_path, monkeypatch, fake)                    # a new process, the same day
    assert restarted.get_account()["equity"] == 2400.0               # still measured from 10,000, not rebased to 9,900
    saved["date"] = "2000-01-01"
    (tmp_path / "logs" / "live_capital.json").write_text(json.dumps(saved))
    next_day = make(tmp_path, monkeypatch, fake)
    assert next_day.get_account()["equity"] == 2475.0                # a fresh base: 25% of 9,900


def test_choosing_another_account_the_same_day_starts_a_new_base(tmp_path, monkeypatch):
    fake = FakeClient()
    set_equity(fake, 10_000.0)
    b = make(tmp_path, monkeypatch, fake, SCHWAB_ACCOUNT_INDEX=0)
    assert b.get_account()["equity"] == 2500.0
    other = make(tmp_path, monkeypatch, fake, SCHWAB_ACCOUNT_INDEX=1)       # same logs folder, a different account
    set_equity(fake, 80_000.0)                                       # ...which holds far more
    assert other.get_account()["equity"] == 20_000.0                 # a base of its own, not 2,500 + 70,000


def test_an_unknown_total_value_lets_nothing_through(tmp_path, monkeypatch):
    fake = FakeClient()
    set_equity(fake, None)                                           # Schwab sent no total value
    acct = make(tmp_path, monkeypatch, fake).get_account()
    assert (acct["equity"], acct["cash"], acct["buying_power"], acct["options_buying_power"]) == (0.0, 0.0, 0.0, 0.0)
    assert acct["raw_buying_power"] == 50000.0


def test_an_unwritable_state_file_keeps_the_cap_in_memory(tmp_path, monkeypatch):
    fake = FakeClient()
    set_equity(fake, 10_000.0)
    b = make(tmp_path, monkeypatch, fake)
    (tmp_path / "logs").write_text("a file where the folder should be")
    assert b.get_account()["equity"] == 2500.0
    set_equity(fake, 9_000.0)
    assert b.get_account()["equity"] == 1500.0


def test_the_cap_can_be_switched_off_by_config(tmp_path, monkeypatch):
    acct = make(tmp_path, monkeypatch, LIVE_CAPITAL_CAP_ENABLED=False).get_account()
    assert acct["equity"] == 25000.5 and "raw_equity" not in acct


# ---------------------------------------------------------------- the contract cap

def test_an_opening_order_is_clamped_to_one_contract(tmp_path, monkeypatch, caplog):
    b = make(tmp_path, monkeypatch)
    with caplog.at_level(logging.WARNING):
        order = b.place_credit_spread("SPX", EXP, "P", 7610, 7605, qty=3, limit_credit=1.5, root="SPXW", client_tag="0dte-astra-a")
    assert order["qty"] == 1 and [leg["quantity"] for leg in order["raw"]["orderLegCollection"]] == [1, 1]
    assert any("LIVE CAP" in r.message and "asked for 3" in r.message for r in caplog.records)


def test_a_live_opening_order_is_clamped_before_it_is_sent(tmp_path, monkeypatch):
    fake = LiveFake()
    b = make(tmp_path, monkeypatch, fake, live=True)
    select(tmp_path)
    b.place_credit_spread("SPX", EXP, "P", 7610, 7605, qty=5, limit_credit=1.5, root="SPXW")
    (_, sent), = fake.placed
    assert [leg["quantity"] for leg in sent["orderLegCollection"]] == [1, 1]
    assert [leg["instruction"] for leg in sent["orderLegCollection"]] == ["SELL_TO_OPEN", "BUY_TO_OPEN"]


def test_the_cap_value_is_configurable_and_orders_within_it_are_untouched(tmp_path, monkeypatch):
    b = make(tmp_path, monkeypatch, LIVE_MAX_CONTRACTS_PER_TRADE=2)
    assert b.place_credit_spread("SPX", EXP, "P", 7610, 7605, qty=4, limit_credit=1.5, root="SPXW")["qty"] == 2
    assert b.place_credit_spread("SPX", EXP, "P", 7610, 7605, qty=1, limit_credit=1.5, root="SPXW")["qty"] == 1


def test_closing_orders_are_never_clamped(tmp_path, monkeypatch):
    b = make(tmp_path, monkeypatch)
    order = b.place_close_spread("SPX", EXP, "P", 7610, 7605, qty=3, limit_debit=0.5, root="SPXW")
    assert order["qty"] == 3 and [leg["quantity"] for leg in order["raw"]["orderLegCollection"]] == [3, 3]


def test_without_the_cap_in_the_config_nothing_is_clamped(tmp_path, monkeypatch):
    monkeypatch.setenv("SCHWAB_LIVE_ORDERS", "false")
    cfg = make_config(tmp_path, SCHWAB_MODE="dry_run")
    b = Broker(cfg, dry_run=True)
    assert b.place_credit_spread("SPX", EXP, "P", 7610, 7605, qty=3, limit_credit=1.5, root="SPXW")["qty"] == 3


# ---------------------------------------------------------------- sizing, config and the start report

def test_the_strategies_own_sizing_is_capped_by_hard_max_contracts(monkeypatch):
    monkeypatch.setattr(risk_manager.config, "TESTING_MODE", False)
    monkeypatch.setattr(risk_manager.config, "MAX_CONTRACTS_PER_TRADE", 6)
    monkeypatch.setattr(risk_manager.config, "HARD_MAX_CONTRACTS", None, raising=False)
    big = risk_manager.contracts_for(100_000, 5, 3.00, 0.0)
    assert big > 1
    monkeypatch.setattr(risk_manager.config, "HARD_MAX_CONTRACTS", 1, raising=False)
    assert risk_manager.contracts_for(100_000, 5, 3.00, 0.0) == 1
    assert risk_manager.contracts_for(100_000, 5, 3.00, 0.0, news_day=True, half_size=True) == 1
    assert risk_manager.contracts_for(2_000, 5, 3.00, 900.0) == 0           # a cap never turns "no room" into a trade


def config_values(**env) -> dict:
    """The go-live constants of config.py as a fresh process computes them (the folder's .env is ignored)."""
    clean = {k: v for k, v in os.environ.items() if k not in ("SCHWAB_MODE", "LIVE_CAPITAL_FLOOR_USD", "LIVE_CAPITAL_PCT", "LIVE_MAX_CONTRACTS_PER_TRADE")}
    code = ("import dotenv; dotenv.load_dotenv = lambda *a, **k: False; import config, json; "
            "print(json.dumps([config.LIVE_CAPITAL_CAP_ENABLED, config.LIVE_CAPITAL_FLOOR_USD, config.LIVE_CAPITAL_PCT, "
            "config.LIVE_MAX_CONTRACTS_PER_TRADE, config.HARD_MAX_CONTRACTS]))")
    out = subprocess.run([sys.executable, "-c", code], cwd=str(FOLDER), env={**clean, **env}, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return dict(zip(("enabled", "floor", "pct", "max", "hard"), json.loads(out.stdout)))


def test_the_shipped_config_values():
    assert config_values(SCHWAB_MODE="sim") == {"enabled": False, "floor": 1000.0, "pct": 0.25, "max": 1, "hard": None}
    assert config_values(SCHWAB_MODE="dry_run") == {"enabled": True, "floor": 1000.0, "pct": 0.25, "max": 1, "hard": 1}
    assert config_values(SCHWAB_MODE="live") == {"enabled": True, "floor": 1000.0, "pct": 0.25, "max": 1, "hard": 1}
    assert config_values() == {"enabled": False, "floor": 1000.0, "pct": 0.25, "max": 1, "hard": None}        # no mode set = sim
    raised = config_values(SCHWAB_MODE="live", LIVE_MAX_CONTRACTS_PER_TRADE="2", LIVE_CAPITAL_FLOOR_USD="2500", LIVE_CAPITAL_PCT="0.5")
    assert raised == {"enabled": True, "floor": 2500.0, "pct": 0.5, "max": 2, "hard": 2}


def test_the_scheduler_prints_the_broker_lines_and_never_fails_on_them():
    from scheduler import Bot

    class Brk:
        def account_report_lines(self):
            return ["Account: ...4321 [selected]"]

    class Boom:
        def account_report_lines(self):
            raise RuntimeError("no token")

    stub = type("Stub", (), {})()
    stub.broker = Brk()
    assert Bot._broker_report_lines(stub) == ["Account: ...4321 [selected]"]
    stub.broker = Boom()
    assert Bot._broker_report_lines(stub) == ["Broker notes unavailable (RuntimeError)"]
    stub.broker = object()                                           # the simulator's broker has no such method
    assert Bot._broker_report_lines(stub) == []


def test_an_unreadable_first_read_of_the_day_does_not_poison_the_capital_base(tmp_path, monkeypatch):
    fake = FakeClient()
    set_equity(fake, None)
    b = make(tmp_path, monkeypatch, fake)
    assert b.get_account()["equity"] == 0.0                          # nothing usable while the total value is unknown
    assert not (tmp_path / "logs" / "live_capital.json").exists()    # ...and no base was recorded from it
    set_equity(fake, 10_000.0)
    assert b.get_account()["equity"] == 2500.0                       # not the whole 10,000 measured from a base of 0


def test_the_real_start_report_carries_the_broker_lines(tmp_path, monkeypatch, caplog):
    import config
    import scheduler
    from fake_broker import FakeBroker
    from conftest import et
    from risk_manager import RiskManager
    fake = FakeBroker(equity=10_000, today=datetime(2026, 9, 17).date())
    fake.account_report_lines = lambda: ["Account: ...4321 (MARGIN) [selected] -- DRY-RUN: orders are logged, not sent",
                                         "Contracts: at most 1 per opening order (LIVE_MAX_CONTRACTS_PER_TRADE)"]
    monkeypatch.setattr(scheduler, "LOG_DIR", tmp_path)
    monkeypatch.setattr(scheduler, "Broker", lambda *a, **k: fake)
    monkeypatch.setattr(config, "TICK_SECONDS", 15)
    bot = scheduler.Bot(dry_run=False, once=True)
    bot.today = datetime(2026, 9, 17).date()
    bot.risk = RiskManager(10_000)
    caplog.set_level("INFO")
    bot.report_start(et(9, 25), bot.broker.get_account(), [])
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "Account: ...4321 (MARGIN) [selected]" in text and "Contracts: at most 1 per opening order" in text



# ---------------------------------------------------------------- review round 2 (2026-10-15)

def test_a_timeout_after_an_accepted_order_is_never_followed_by_a_second_order(tmp_path, monkeypatch):
    import httpx

    class TimesOut(LiveFake):
        def place_order(self, account_hash, order_json):
            self.placed.append((account_hash, order_json))
            raise httpx.ReadTimeout("no reply")                      # Schwab may well have accepted it

    fake = TimesOut()
    b = make(tmp_path, monkeypatch, fake, live=True)
    select(tmp_path)
    with pytest.raises(BrokerError, match="NOT re-sent"):
        b.place_credit_spread("SPX", EXP, "P", 7610, 7605, qty=1, limit_credit=1.5, root="SPXW")
    assert len(fake.placed) == 1                                     # one POST, not three


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_an_error_status_after_the_post_is_not_retried_either(tmp_path, monkeypatch, status):
    class Rejects(LiveFake):
        def place_order(self, account_hash, order_json):
            self.placed.append((account_hash, order_json))
            return _resp(status, {"message": "try later"})

    fake = Rejects()
    b = make(tmp_path, monkeypatch, fake, live=True)
    select(tmp_path)
    with pytest.raises(BrokerError, match="NOT re-sent"):
        b.place_credit_spread("SPX", EXP, "P", 7610, 7605, qty=1, limit_credit=1.5, root="SPXW")
    assert len(fake.placed) == 1


def test_when_the_reply_is_lost_but_the_order_rests_at_schwab_the_bot_uses_that_order(tmp_path, monkeypatch):
    import httpx

    class LostReply(LiveFake):
        def place_order(self, account_hash, order_json):
            self.placed.append((account_hash, order_json))
            self.orders_payload = [{
                "orderId": 4242, "status": "WORKING", "quantity": 1.0, "filledQuantity": 0.0, "price": 1.5, "orderType": "NET_CREDIT",
                "duration": "DAY", "orderStrategyType": "SINGLE", "complexOrderStrategyType": "VERTICAL",
                "orderLegCollection": [
                    {"instruction": "SELL_TO_OPEN", "quantity": 1.0, "instrument": {"symbol": "SPXW  260917P07610000", "assetType": "OPTION"}},
                    {"instruction": "BUY_TO_OPEN", "quantity": 1.0, "instrument": {"symbol": "SPXW  260917P07605000", "assetType": "OPTION"}}]}]
            raise httpx.ReadTimeout("no reply")

    fake = LostReply()
    b = make(tmp_path, monkeypatch, fake, live=True)
    select(tmp_path)
    order = b.place_credit_spread("SPX", EXP, "P", 7610, 7605, qty=1, limit_credit=1.5, root="SPXW")
    assert str(order["id"]) == "4242" and len(fake.placed) == 1
    fake.orders_payload[0]["price"] = 1.4                            # a resting order that is NOT this one is not adopted
    (tmp_path / "b2").mkdir()
    other = LostReply()
    b2 = make(tmp_path / "b2", monkeypatch, other, live=True)
    select(tmp_path / "b2")
    other_place = other.place_order

    def place_then_change(account_hash, order_json):
        try:
            return other_place(account_hash, order_json)
        finally:
            other.orders_payload[0]["price"] = 1.4
    other.place_order = place_then_change
    with pytest.raises(BrokerError, match="NOT re-sent"):
        b2.place_credit_spread("SPX", EXP, "P", 7610, 7605, qty=1, limit_credit=1.5, root="SPXW")


@pytest.mark.parametrize("bad", [0, -1, "abc", ""])
def test_an_invalid_contract_cap_stops_the_bot_instead_of_switching_the_cap_off(tmp_path, monkeypatch, bad):
    monkeypatch.setenv("SCHWAB_LIVE_ORDERS", "false")
    with pytest.raises(BrokerError, match="LIVE_MAX_CONTRACTS_PER_TRADE"):
        Broker(make_config(tmp_path, SCHWAB_MODE="dry_run", **{**LIVE_KEYS, "LIVE_MAX_CONTRACTS_PER_TRADE": bad}), dry_run=True)


@pytest.mark.parametrize("mode", ["paper", "dry-run", "", "Live2", "production"])
def test_an_unrecognised_mode_is_refused(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("SCHWAB_LIVE_ORDERS", "true")
    with pytest.raises(BrokerError, match="not one of sim, dry_run, live"):
        Broker(make_config(tmp_path, SCHWAB_MODE=mode), dry_run=False)


def test_a_hard_cap_of_zero_means_no_contracts_not_no_cap(monkeypatch):
    monkeypatch.setattr(risk_manager.config, "TESTING_MODE", False)
    monkeypatch.setattr(risk_manager.config, "MAX_CONTRACTS_PER_TRADE", 6)
    monkeypatch.setattr(risk_manager.config, "HARD_MAX_CONTRACTS", 0, raising=False)
    assert risk_manager.contracts_for(100_000, 5, 3.00, 0.0) == 0


def run_config(**env):
    clean = {k: v for k, v in os.environ.items() if k not in ("SCHWAB_MODE", "LIVE_CAPITAL_FLOOR_USD", "LIVE_CAPITAL_PCT", "LIVE_MAX_CONTRACTS_PER_TRADE")}
    code = "import dotenv; dotenv.load_dotenv = lambda *a, **k: False; import config; print(config.SCHWAB_MODE, config.HARD_MAX_CONTRACTS, config.LIVE_CAPITAL_PCT)"
    return subprocess.run([sys.executable, "-c", code], cwd=str(FOLDER), env={**clean, **env}, capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("env,named", [
    ({"SCHWAB_MODE": "live", "LIVE_MAX_CONTRACTS_PER_TRADE": "0"}, "LIVE_MAX_CONTRACTS_PER_TRADE"),
    ({"SCHWAB_MODE": "live", "LIVE_MAX_CONTRACTS_PER_TRADE": "-2"}, "LIVE_MAX_CONTRACTS_PER_TRADE"),
    ({"SCHWAB_MODE": "dry_run", "LIVE_MAX_CONTRACTS_PER_TRADE": "abc"}, "LIVE_MAX_CONTRACTS_PER_TRADE"),
    ({"SCHWAB_MODE": "live", "LIVE_MAX_CONTRACTS_PER_TRADE": "1.5"}, "LIVE_MAX_CONTRACTS_PER_TRADE"),
    ({"SCHWAB_MODE": "live", "LIVE_CAPITAL_PCT": "25"}, "LIVE_CAPITAL_PCT"),
    ({"SCHWAB_MODE": "live", "LIVE_CAPITAL_PCT": "0"}, "LIVE_CAPITAL_PCT"),
    ({"SCHWAB_MODE": "live", "LIVE_CAPITAL_PCT": "nan"}, "LIVE_CAPITAL_PCT"),
    ({"SCHWAB_MODE": "live", "LIVE_CAPITAL_FLOOR_USD": "-5"}, "LIVE_CAPITAL_FLOOR_USD"),
    ({"SCHWAB_MODE": "live", "LIVE_CAPITAL_FLOOR_USD": "inf"}, "LIVE_CAPITAL_FLOOR_USD"),
    ({"SCHWAB_MODE": "paper"}, "SCHWAB_MODE"),
    ({"SCHWAB_MODE": "dry-run"}, "SCHWAB_MODE"),
])
def test_the_config_refuses_values_that_would_switch_a_cap_off_or_widen_it(env, named):
    out = run_config(**env)
    assert out.returncode != 0 and named in out.stderr and "ValueError" in out.stderr


def test_the_simulator_ignores_bad_limits_and_a_blank_mode_is_the_simulator():
    out = run_config(SCHWAB_MODE="sim", LIVE_MAX_CONTRACTS_PER_TRADE="abc", LIVE_CAPITAL_PCT="25")
    assert out.returncode == 0 and out.stdout.split() == ["sim", "None", "0.25"], out.stderr
    assert run_config(SCHWAB_MODE="").stdout.split()[0] == "sim"
    assert run_config(SCHWAB_MODE="  LIVE ", LIVE_MAX_CONTRACTS_PER_TRADE=" 2 ").stdout.split()[:2] == ["live", "2"]


def make_bot(tmp_path, monkeypatch, fake):
    import config
    import scheduler
    from risk_manager import RiskManager
    monkeypatch.setattr(scheduler, "LOG_DIR", tmp_path)
    monkeypatch.setattr(scheduler, "Broker", lambda *a, **k: fake)
    monkeypatch.setattr(config, "TICK_SECONDS", 15)
    bot = scheduler.Bot(dry_run=False, once=True)
    bot.today = datetime(2026, 9, 17).date()
    bot.risk = RiskManager(10_000)
    return bot


def test_an_entry_cancel_that_fails_because_the_order_just_filled_books_the_fill(tmp_path, monkeypatch):
    from fake_broker import FakeBroker
    import scheduler as scheduler_mod

    class FilledDuringCancel(FakeBroker):
        def cancel_order(self, order_id):
            self.orders[order_id].update(status="filled", filled_qty=1)
            raise scheduler_mod.BrokerError("Schwab cancel_order failed: HTTP 400 order already filled")

    fake = FilledDuringCancel(equity=10_000, today=datetime(2026, 9, 17).date())
    fake.fill_entries = False
    bot = make_bot(tmp_path, monkeypatch, fake)
    order = fake.place_credit_spread("SPX", fake.today, "P", 7610, 7605, 1, 1.5, root="SPXW")
    final = bot._work_entry(order, 1.5, 1.4, step=0.05, wait_sec=0, timeout_sec=0)
    assert final["status"] == "filled" and final["filled_qty"] == 1


def test_an_entry_cancel_that_leaves_the_order_pending_waits_for_it_to_settle(tmp_path, monkeypatch):
    from fake_broker import FakeBroker

    class PendingThenFilled(FakeBroker):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.waited = 0

        def wait_for_fill(self, order_id, timeout_sec, poll_sec=2.0):
            if timeout_sec == 0:                                     # the entry ladder's own (immediate) wait
                return self.get_order(order_id)
            self.waited += 1
            return {**self.get_order(order_id), "status": "filled", "filled_qty": 1}

        def cancel_order(self, order_id):
            self.orders[order_id]["status"] = "pending"             # PENDING_CANCEL as the broker normalises it
            return True

    fake = PendingThenFilled(equity=10_000, today=datetime(2026, 9, 17).date())
    fake.fill_entries = False
    bot = make_bot(tmp_path, monkeypatch, fake)
    order = fake.place_credit_spread("SPX", fake.today, "P", 7610, 7605, 1, 1.5, root="SPXW")
    final = bot._work_entry(order, 1.5, 1.4, step=0.05, wait_sec=0, timeout_sec=0)
    assert fake.waited == 1 and final["status"] == "filled" and final["filled_qty"] == 1


def test_the_days_loss_reference_is_the_capital_cap_whatever_state_was_restored(tmp_path, monkeypatch):
    from fake_broker import FakeBroker
    fake = FakeBroker(equity=2_500, today=datetime(2026, 9, 17).date())
    fake.get_account = lambda: {"equity": 2_350.0, "cash": 2_350.0, "buying_power": 2_350.0, "options_buying_power": 2_350.0,
                                "account_id": "x", "capital_cap": 2_500.0, "raw_equity": 9_850.0}
    bot = make_bot(tmp_path, monkeypatch, fake)
    monkeypatch.setattr(bot, "run", lambda now: None)                 # start() ends in the main loop
    bot.start()
    assert bot.risk.state.start_equity == 2_500.0                    # not the 2,350 a mid-day restart sees, and not an old sim figure
    plain = FakeBroker(equity=10_000, today=datetime(2026, 9, 17).date())          # no cap (Alpaca, the simulator): untouched
    (tmp_path / "plain").mkdir()
    other = make_bot(tmp_path / "plain", monkeypatch, plain)
    monkeypatch.setattr(other, "run", lambda now: None)
    other.start()
    assert other.risk.state.start_equity == 10_000


def test_the_equity_history_records_the_real_account_value(tmp_path, monkeypatch):
    fake = FakeClient()
    b = make(tmp_path, monkeypatch, fake)
    b.record_daily_equity()
    rows = (tmp_path / "logs" / "equity_history.csv").read_text().strip().splitlines()
    assert rows[1].split(",")[1] == "25000.50"                       # the real total value (the capped figure is 6,250.12)


def test_the_capital_base_follows_the_new_york_date_and_rolls_over_in_a_running_process(tmp_path, monkeypatch):
    from zoneinfo import ZoneInfo
    stamp = {"now": datetime(2026, 10, 15, 23, 30, tzinfo=ZoneInfo("America/New_York"))}      # 03:30 UTC on the 16th

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return stamp["now"].astimezone(tz) if tz else stamp["now"].replace(tzinfo=None)

    monkeypatch.setattr(broker_mod, "datetime", Clock)
    fake = FakeClient()
    set_equity(fake, 10_000.0)
    b = make(tmp_path, monkeypatch, fake)
    assert b.get_account()["equity"] == 2500.0
    assert json.loads((tmp_path / "logs" / "live_capital.json").read_text())["date"] == "2026-10-15"       # not the UTC date
    set_equity(fake, 9_000.0)
    stamp["now"] = datetime(2026, 10, 16, 9, 30, tzinfo=ZoneInfo("America/New_York"))
    assert b.get_account()["equity"] == 2250.0                       # the next ET day: a fresh base in the SAME process (25% of 9,000)
    assert json.loads((tmp_path / "logs" / "live_capital.json").read_text())["date"] == "2026-10-16"


def test_the_cap_is_sized_from_the_total_value_not_from_equity(tmp_path, monkeypatch):
    fake = FakeClient()
    bal = fake.account_payload["securitiesAccount"]["currentBalances"]
    bal.update(liquidationValue=40_000.0, equity=8_000.0)           # on a margin account the two differ
    assert make(tmp_path, monkeypatch, fake).get_account()["capital_cap"] == 10_000.0
    bal.pop("liquidationValue")                                       # no total value: fall back to equity, as the bot always did
    assert make(tmp_path / "b", monkeypatch, fake).get_account()["capital_cap"] == 2_000.0


class TwoAccounts(FakeClient):
    """Account HASH0 holds 25,000.50, account HASH1 holds 4,400."""

    def get_account(self, account_hash, *, fields=None):
        self._rec("get_account", account_hash, fields=fields)
        payload = json.loads(json.dumps(self.account_payload))
        if account_hash == "HASH1":
            payload["securitiesAccount"]["currentBalances"].update(liquidationValue=4_400.0, equity=4_400.0, cashBalance=4_400.0)
        return _resp(200, payload)


def test_a_dry_run_bot_with_a_selection_reads_and_sizes_from_the_selected_account(tmp_path, monkeypatch):
    fake = TwoAccounts()
    b = make(tmp_path, monkeypatch, fake, SCHWAB_ACCOUNT_INDEX=0)
    select(tmp_path, "HASH1", "4321")
    assert b._hash() == "HASH1"
    acct = b.get_account()
    assert acct["capital_cap"] == 1_100.0 and acct["raw_equity"] == 4_400.0                     # HASH1's balances, not HASH0's
    assert {c[1][0] for c in fake.calls if c[0] == "get_account"} == {"HASH1"}
    assert "[selected]" in b.account_report_lines()[0]


def test_the_broker_reads_the_file_the_selector_writes_by_default(tmp_path, monkeypatch):
    import schwab_select_account
    monkeypatch.setattr(broker_mod, "HERE", tmp_path)
    monkeypatch.setattr(schwab_select_account, "HERE", tmp_path)
    monkeypatch.setenv("SCHWAB_LIVE_ORDERS", "false")
    cfg = make_config(tmp_path, SCHWAB_MODE="dry_run", **LIVE_KEYS)
    del cfg.SCHWAB_ACCOUNT_FILE                                      # the shipped config sets none: the default name is used
    b = Broker(cfg, dry_run=True)
    b._client = FakeClient()
    schwab_accounts.write_selection(schwab_accounts.selection_path(schwab_select_account.HERE),
                                    {"hash_value": "HASH1", "last4": "4321", "type": "", "selected_at": "x"})
    assert b._hash() == "HASH1"


def test_a_changed_cap_setting_starts_a_new_base(tmp_path, monkeypatch):
    fake = FakeClient()
    set_equity(fake, 10_000.0)
    assert make(tmp_path, monkeypatch, fake).get_account()["equity"] == 2500.0
    assert make(tmp_path, monkeypatch, fake, LIVE_CAPITAL_PCT=0.5).get_account()["equity"] == 5000.0       # not silently the old 25%
    assert make(tmp_path, monkeypatch, fake, LIVE_CAPITAL_FLOOR_USD=6000.0).get_account()["equity"] == 6000.0


def test_the_account_hash_is_not_written_to_the_capital_file(tmp_path, monkeypatch):
    make(tmp_path, monkeypatch).get_account()
    text = (tmp_path / "logs" / "live_capital.json").read_text()
    assert "HASH0" not in text and len(json.loads(text)["account"]) == 12


def test_a_shared_account_refuses_new_orders_but_still_reads_cancels_and_closes(tmp_path, monkeypatch):
    """2026-10-26: schwab/<Person>/0dte layout -- two folders selecting one account must not double the exposure, but a bot must ALWAYS be
    able to manage and close what it already holds (a restart with an open spread must not strand it)."""
    micah = tmp_path / "schwab" / "Micah" / "0dte"
    dawson = tmp_path / "schwab" / "Dawson" / "0dte"
    micah.mkdir(parents=True)
    dawson.mkdir(parents=True)
    for folder in (micah, dawson):
        schwab_accounts.write_selection(schwab_accounts.selection_path(folder),
                                        {"hash_value": "HASH1", "last4": "4321", "type": "MARGIN", "selected_at": "t"})
    fake = LiveFake()
    monkeypatch.setenv("SCHWAB_LIVE_ORDERS", "true")
    monkeypatch.delenv("SCHWAB_ALLOW_SHARED_ACCOUNT", raising=False)
    cfg = make_config(tmp_path, SCHWAB_MODE="live", SCHWAB_ACCOUNT_FILE=str(schwab_accounts.selection_path(dawson)), **LIVE_KEYS)
    b = Broker(cfg, dry_run=False, log=logging.getLogger("test.golive"))
    b._client = fake
    assert b.get_account()["account_id"] == "12345678"                                # reads work (a restart can see its positions)
    assert any("also the selected account of Micah" in line and "NEW orders are refused" in line for line in b.account_report_lines())
    b.get_positions()
    b.get_open_orders()
    with pytest.raises(BrokerError, match="NO new orders: account ...4321 is also the selected account of Micah"):
        b.place_credit_spread("SPX", EXP, "P", 7610, 7605, qty=1, limit_credit=1.5, root="SPXW")
    b.place_close_spread("SPX", EXP, "P", 7610, 7605, qty=1, limit_debit=0.5, root="SPXW")   # a close always goes out
    assert [h for h, _ in fake.placed] == ["HASH1"]
    fresh = LiveFake()                                                                # the very first call of a process being an order
    b2 = Broker(cfg, dry_run=False, log=logging.getLogger("test.golive"))
    b2._client = fresh
    with pytest.raises(BrokerError, match="NO new orders"):
        b2.place_credit_spread("SPX", EXP, "P", 7610, 7605, qty=1, limit_credit=1.5, root="SPXW")
    assert fresh.placed == []
    dry = Broker(make_config(tmp_path, SCHWAB_MODE="dry_run", SCHWAB_ACCOUNT_FILE=str(schwab_accounts.selection_path(dawson)), **LIVE_KEYS),
                 dry_run=True)
    dry._client = FakeClient()
    assert dry.place_credit_spread("SPX", EXP, "P", 7610, 7605, qty=1, limit_credit=1.5, root="SPXW")["status"] == "dry_run"   # sends nothing
    assert any("once LIVE, new orders would be refused" in line for line in dry.account_report_lines())     # never claims it refused
    monkeypatch.setenv("SCHWAB_ALLOW_SHARED_ACCOUNT", "true")
    b3 = Broker(cfg, dry_run=False, log=logging.getLogger("test.golive"))
    b3._client = fake
    b3.place_credit_spread("SPX", EXP, "P", 7610, 7605, qty=1, limit_credit=1.5, root="SPXW")
    assert [h for h, _ in fake.placed] == ["HASH1", "HASH1"]
    assert any("ALSO traded by Micah" in line and "two bots on one account" in line for line in b3.account_report_lines())


def test_a_negative_account_index_is_refused_at_start_up(tmp_path, monkeypatch):
    monkeypatch.setenv("SCHWAB_LIVE_ORDERS", "false")
    with pytest.raises(BrokerError, match="counts from 0"):
        Broker(make_config(tmp_path, SCHWAB_MODE="dry_run", SCHWAB_ACCOUNT_INDEX=-1), dry_run=True)


# ---------------------------------------------------------------- the SPX $0.05 price grid, live (2026-11-03)

def _raw_close(order_id, status, price, desc=None, filled=0.0, order_type="NET_DEBIT"):
    raw = {"orderId": order_id, "status": status, "orderType": order_type, "price": price, "quantity": 1.0, "filledQuantity": filled,
           "duration": "DAY", "session": "NORMAL", "orderStrategyType": "SINGLE", "complexOrderStrategyType": "VERTICAL",
           "enteredTime": "2026-11-03T16:07:16+0000",
           "orderLegCollection": [
               {"legId": 1, "instruction": "BUY_TO_CLOSE", "quantity": 1.0,
                "instrument": {"assetType": "OPTION", "symbol": "SPXW  260917P07610000"}},
               {"legId": 2, "instruction": "SELL_TO_CLOSE", "quantity": 1.0,
                "instrument": {"assetType": "OPTION", "symbol": "SPXW  260917P07605000"}}]}
    if desc:
        raw["statusDescription"] = desc
    return raw


class CloseFake(LiveFake):
    """Numbers the orders it is sent 777, 778, ...; a test scripts what get_order says about each (default: filled)."""

    def __init__(self):
        super().__init__()
        self.next_id = 777
        self.canceled: list[str] = []

    def place_order(self, account_hash, order_json):
        self.placed.append((account_hash, order_json))
        oid, self.next_id = self.next_id, self.next_id + 1
        self.order_by_id.setdefault(str(oid), _raw_close(oid, "FILLED", float(order_json["price"]), filled=1.0))
        return _resp(201, None, {"Location": f"https://api.schwabapi.com/trader/v1/accounts/x/orders/{oid}"})

    def cancel_order(self, order_id, account_hash):
        self.canceled.append(str(order_id))
        self.order_by_id[str(order_id)]["status"] = "CANCELED"
        return _resp(200, None)


def test_a_rejected_close_carries_schwabs_reason_and_was_sent_on_the_grid(tmp_path, monkeypatch, caplog):
    fake = CloseFake()
    fake.order_by_id["777"] = _raw_close(777, "REJECTED", 1.80, "Option order price rounded to nearest 0.05 (not a penny pilot)")
    b = make(tmp_path, monkeypatch, fake, live=True, CLOSE_SLIPPAGE=0.03)
    select(tmp_path)
    with caplog.at_level(logging.INFO):
        with pytest.raises(BrokerError, match="Schwab says: Option order price rounded to nearest 0.05"):
            b.close_spread_at_market("SPX", EXP, "P", 7610, 7605, qty=1, root="SPXW", time_in_force="day")
    assert [o["price"] for _, o in fake.placed] == ["1.80"]             # ask-side 1.80 + 0.03 -> 1.80, never 1.83
    sent = [r for r in caplog.records if "[LIVE]" in r.getMessage() and "sending" in r.getMessage()]
    assert sent and all(getattr(r, "keep", False) for r in sent)        # the order that was sent reaches the log file


def test_a_close_that_does_not_fill_is_re_sent_one_tick_higher(tmp_path, monkeypatch):
    fake = CloseFake()
    fake.order_by_id["777"] = _raw_close(777, "WORKING", 1.80)           # rests, is cancelled; the retry fills
    b = make(tmp_path, monkeypatch, fake, live=True, CLOSE_SLIPPAGE=0.03)
    select(tmp_path)
    order = b.close_spread_at_market("SPX", EXP, "P", 7610, 7605, qty=1, root="SPXW", time_in_force="day")
    assert order["status"] == "filled" and order["id"] == "778"
    assert [o["price"] for _, o in fake.placed] == ["1.80", "1.85"]     # not 1.83, and not 1.80 again
    assert fake.canceled == ["777"]


def test_a_live_re_price_of_an_spx_order_lands_on_the_grid_and_a_no_op_is_not_sent(tmp_path, monkeypatch):
    class Replaces(LiveFake):
        def __init__(self):
            super().__init__()
            self.replaced: list[tuple[str, dict]] = []

        def replace_order(self, account_hash, order_id, order_json):
            self.replaced.append((str(order_id), order_json))
            return _resp(201, None, {"Location": "https://api.schwabapi.com/trader/v1/accounts/x/orders/556"})

    fake = Replaces()
    fake.order_by_id["555"] = _raw_close(555, "WORKING", 0.85, order_type="NET_CREDIT")
    fake.order_by_id["556"] = _raw_close(556, "WORKING", 0.80, order_type="NET_CREDIT")
    b = make(tmp_path, monkeypatch, fake, live=True)
    select(tmp_path)
    assert b.replace_order_price("555", 0.86)["id"] == "555"            # 0.86 snaps to 0.85, the order's own price: nothing sent
    assert fake.replaced == []
    order = b.replace_order_price("555", 0.83)                           # 0.83 -> 0.80
    assert order["id"] == "556" and fake.replaced == [("555", fake.replaced[0][1])]
    assert fake.replaced[0][1]["price"] == "0.80"


def test_the_live_bots_log_callable_accepts_the_keep_tag_and_a_plain_callable_drops_it(tmp_path, monkeypatch, caplog):
    """scheduler.py builds the broker with log=log.info (a bound Logger method, wrapped in _CallableLog), not a Logger:
    the keep tag on the 'sending' lines must reach the log and never raise. Caught by review on 2026-11-03."""
    fake = CloseFake()
    b = make(tmp_path, monkeypatch, fake, live=True)
    b.log = broker_mod._CallableLog(logging.getLogger("test.golive.callable").info)
    select(tmp_path)
    with caplog.at_level(logging.INFO):
        order = b.place_credit_spread("SPX", EXP, "P", 7610, 7605, qty=1, limit_credit=0.88, root="SPXW")
    assert order["status"] == "filled" and fake.placed[0][1]["price"] == "0.85"
    sent = [r for r in caplog.records if "sending" in r.getMessage()]
    assert sent and all(getattr(r, "keep", False) for r in sent)
    b.log.debug("the adapter answers debug too")
    lines: list[str] = []
    b.log = broker_mod._CallableLog(lines.append)                       # a plain callable: the tag is dropped, nothing raises
    b.place_credit_spread("SPX", EXP, "P", 7610, 7605, qty=1, limit_credit=0.88, root="SPXW")
    assert any("sending" in line for line in lines)
