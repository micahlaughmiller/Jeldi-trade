import pytest

import config_45dte
from risk_manager import RiskManager


def spec(max_loss=3.50, symbol="XYZ"):
    return {"broker_symbol": symbol, "max_loss": max_loss}


def spread(symbol, qty, max_loss):
    return {"broker_symbol": symbol, "qty": qty, "max_loss": max_loss}


@pytest.fixture
def rm():
    return RiskManager(config_45dte)


@pytest.mark.parametrize("equity, cap", [(9_999, 3), (10_000, 5), (29_999, 5), (30_000, 10), (49_999, 10), (50_000, 15), (1_000_000, 15)])
def test_tier_caps(rm, equity, cap):
    assert rm.tier_cap(equity) == cap


def test_sizing_uses_4pct_budget_capped_by_tier(rm):
    # 100k * 4% = 4000 / 350 = 11 -> tier cap 15 leaves 11
    assert rm.size_position(100_000, 3.50, 0.0)[0] == 11
    # 100k budget 4000 / 150 = 26 -> capped to 15
    assert rm.size_position(100_000, 1.50, 0.0)[0] == 15
    # 20k budget 800 / 350 = 2
    assert rm.size_position(20_000, 3.50, 0.0)[0] == 2


def test_min_contract_override_at_5000_equity(rm):
    qty, note = rm.size_position(5_000, 3.50, 0.0)
    assert qty == 1 and "override" in note
    # override refused when it would breach the 52% cap: 2350 + 350 = 2700 / 5000 = 54%
    qty, _ = rm.size_position(5_000, 3.50, 2_350.0)
    assert qty == 0


def test_override_disabled(monkeypatch):
    monkeypatch.setattr(config_45dte, "ALLOW_MIN_CONTRACT_OVERRIDE", False)
    assert RiskManager(config_45dte).size_position(5_000, 3.50, 0.0)[0] == 0


def test_portfolio_cap_reduces_or_rejects(rm):
    equity = 100_000
    open_spreads = [spread("A", 15, 3.40), spread("B", 15, 3.40), spread("C", 15, 3.40), spread("D", 15, 3.40)]
    pending = [spread("E", 15, 3.40), spread("F", 15, 3.40), spread("G", 15, 3.40), spread("H", 15, 3.40),
               spread("I", 15, 3.40), spread("J", 5, 3.40)]
    open_risk = rm.get_current_portfolio_risk(open_spreads, pending)
    assert open_risk == pytest.approx(140 * 340)
    # 47.6% used; budget 4000/350 -> 11 contracts -> 51.45% fits under the cap untouched
    decision = rm.check_new_entry(spec(3.50), equity, open_spreads, pending, entries_today=0)
    assert decision.allowed and decision.qty == 11
    assert decision.details["portfolio_risk_pct_after"] == pytest.approx(0.5145)
    # 51.45% used; 11 more would be 55.3% -> reduced to the 1 contract that still fits
    pending.append(spread("K", 11, 3.50))
    decision = rm.check_new_entry(spec(3.50, "Y"), equity, open_spreads, pending, entries_today=0)
    assert decision.allowed and decision.qty == 1 and "reduced for portfolio cap" in decision.reason
    # 51.8% used; even one contract breaches 52% -> rejected
    pending.append(spread("Y", 1, 3.50))
    decision = rm.check_new_entry(spec(3.50, "Z"), equity, open_spreads, pending, entries_today=0)
    assert not decision.allowed and "size 0" in decision.reason


def test_get_current_portfolio_risk_rejects_none(rm):
    with pytest.raises(ValueError):
        rm.get_current_portfolio_risk(None)


def test_one_position_per_underlying_and_daily_limit(rm):
    decision = rm.check_new_entry(spec(), 50_000, [spread("XYZ", 1, 3.5)], [], 0)
    assert not decision.allowed and "already" in decision.reason
    decision = rm.check_new_entry(spec(), 50_000, [], [spread("XYZ", 1, 3.5)], 0)
    assert not decision.allowed
    decision = rm.check_new_entry(spec(), 50_000, [], [], entries_today=config_45dte.MAX_NEW_POSITIONS_PER_DAY)
    assert not decision.allowed and "daily entry limit" in decision.reason
    decision = rm.check_new_entry(spec(), 50_000, [], [], 0, blocked_symbols={"XYZ"})
    assert not decision.allowed and "blocked" in decision.reason


def test_breaker_trips_at_three_hits_and_persists_via_callback():
    changes = []
    state = {}
    rm = RiskManager(config_45dte, breaker_state=state, on_change=lambda: changes.append(1))
    assert rm.record_max_loss_hit("a") is False
    assert rm.record_max_loss_hit("a") is False
    assert rm.record_max_loss_hit("b") is False
    assert not rm.breaker_tripped
    assert rm.record_max_loss_hit("c") is True
    assert rm.breaker_tripped and rm.max_loss_hits == 3 and state["tripped"]
    decision = rm.check_new_entry(spec(), 50_000, [], [], 0)
    assert not decision.allowed and "breaker" in decision.reason
    rm.reset_breaker()
    assert not rm.breaker_tripped and rm.max_loss_hits == 0
    assert len(changes) == 4


def test_max_loss_hit_definitions(rm):
    # credit 1.50, max loss 3.50 -> hit at price >= 1.50 + 0.9*3.50 = 4.65
    assert rm.is_max_loss_hit(1.50, 4.65)
    assert not rm.is_max_loss_hit(1.50, 4.60)
    assert not rm.is_max_loss_hit(1.50, None)
    assert rm.is_realized_max_loss(1.50, 4.65)
    assert not rm.is_realized_max_loss(1.50, 4.00)


def test_daily_loss_alert(rm):
    assert rm.daily_loss_alert(10_000, 9_700) == (True, pytest.approx(0.03))
    assert rm.daily_loss_alert(10_000, 9_800)[0] is False
    assert rm.daily_loss_alert(None, 9_800) == (False, 0.0)
