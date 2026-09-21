from datetime import date, timedelta

import pytest

import signal_generator as sg
from fake_broker import FakeBroker, make_quote

TODAY = date(2026, 9, 17)


def days(n: int) -> date:
    return TODAY + timedelta(days=n)


def put_chain(sym: str, exp: date, long_mid_bump: float = 0.0, strikes=None):
    spec = strikes or {
        85: (-0.15, 0.95, 1.05),
        90: (-0.22, 2.10 + long_mid_bump, 2.30 + long_mid_bump),
        95: (-0.30, 3.60, 3.80),
        100: (-0.38, 5.40, 5.60),
        105: (-0.50, 7.90, 8.10),
    }
    return [make_quote(sym, exp, "P", float(k), bid, ask, delta) for k, (delta, bid, ask) in spec.items()]


def row(signal="oversold", strong=False):
    return {"symbol": "XYZ", "broker_symbol": "XYZ", "signal": signal, "strong": strong, "rsi14": 25.0, "rsi28": 28.0}


def broker_with(expirations, chain_exp=None, **chain_kwargs):
    b = FakeBroker()
    b.expirations["XYZ"] = expirations
    b.spots["XYZ"] = 100.0
    exp = chain_exp or expirations[0]
    b.chains[("XYZ", exp, "P")] = put_chain("XYZ", exp, **chain_kwargs)
    return b


class TestChooseExpiration:
    def test_inside_window_closest_to_52(self):
        exp, dte, out, _ = sg.choose_expiration([days(45), days(51), days(59)], TODAY)
        assert (exp, dte, out) == (days(51), 51, False)

    def test_tie_prefers_shorter(self):
        exp, dte, out, _ = sg.choose_expiration([days(50), days(54)], TODAY)
        assert (dte, out) == (50, False)

    def test_nearest_within_tolerance_is_flagged(self):
        exp, dte, out, reason = sg.choose_expiration([days(38), days(66)], TODAY)
        assert (dte, out) == (66, True)
        exp, dte, out, reason = sg.choose_expiration([days(40), days(66)], TODAY)
        assert (dte, out) == (40, True)

    def test_none_when_outside_tolerance(self):
        exp, dte, out, reason = sg.choose_expiration([days(34), days(71)], TODAY)
        assert exp is None and "no expiration" in reason

    def test_boundaries(self):
        assert sg.choose_expiration([days(35)], TODAY)[0] == days(35)
        assert sg.choose_expiration([days(70)], TODAY)[0] == days(70)
        assert sg.choose_expiration([days(45)], TODAY)[2] is False
        assert sg.choose_expiration([days(60)], TODAY)[2] is False


class TestBuildTrade:
    def test_picks_30_delta_and_5_wide_pair_with_accepted_credit(self):
        b = broker_with([days(52)])
        spec = sg.build_trade(b, row(), TODAY)
        assert spec["accepted"] is True
        assert spec["right"] == "P"
        assert spec["short_strike"] == 95.0 and spec["long_strike"] == 90.0
        assert spec["short_delta"] == -0.30
        assert spec["credit"] == 1.50 and spec["bid_side"] == pytest.approx(1.30)
        assert spec["max_loss"] == 3.50 and spec["dte"] == 52 and spec["dte_out_of_range"] is False
        chain_call = next(c for c in b.calls if c[0] == "get_option_chain")
        assert chain_call[2]["spot"] == 100.0
        exp_call = next(c for c in b.calls if c[0] == "get_expirations")
        assert exp_call[1] == ("XYZ", 30, 80)

    def test_call_side_for_overbought(self):
        b = FakeBroker()
        b.expirations["XYZ"] = [days(50)]
        b.chains[("XYZ", days(50), "C")] = [
            make_quote("XYZ", days(50), "C", 105.0, 3.60, 3.80, 0.31),
            make_quote("XYZ", days(50), "C", 110.0, 2.00, 2.20, 0.20),
        ]
        spec = sg.build_trade(b, row("overbought"), TODAY)
        assert spec["accepted"] and spec["right"] == "C"
        assert (spec["short_strike"], spec["long_strike"]) == (105.0, 110.0)
        assert spec["credit"] == 1.60

    def test_missing_pair_falls_back_to_next_delta_candidate(self):
        strikes = {95: (-0.30, 3.60, 3.80), 100: (-0.38, 5.40, 5.60), 105: (-0.50, 7.90, 8.10)}
        b = broker_with([days(52)], strikes=strikes)
        spec = sg.build_trade(b, row(), TODAY)
        assert spec["short_strike"] == 100.0 and spec["long_strike"] == 95.0
        assert spec["credit"] == 1.80 and spec["accepted"]

    def test_no_pair_at_any_width_skips(self):
        strikes = {95: (-0.30, 3.60, 3.80), 105: (-0.38, 5.40, 5.60)}
        b = broker_with([days(52)], strikes=strikes)
        spec = sg.build_trade(b, row(), TODAY)
        assert spec["accepted"] is False and spec["reason"] == "no $5/$2.5/$1-wide pair"

    def test_falls_back_to_2_50_width_with_scaled_credit_floor(self):
        strikes = {95: (-0.30, 3.60, 3.80), 92.5: (-0.25, 2.60, 2.80), 105: (-0.50, 7.90, 8.10)}
        b = broker_with([days(52)], strikes=strikes)
        spec = sg.build_trade(b, row(), TODAY)
        assert spec["accepted"] is True
        assert (spec["short_strike"], spec["long_strike"], spec["width"]) == (95.0, 92.5, 2.5)
        assert spec["credit"] == 1.00 and spec["min_credit"] == 0.75 and spec["max_loss"] == 1.50

    def test_falls_back_to_1_width_with_scaled_credit_floor(self):
        strikes = {95: (-0.30, 3.60, 3.80), 94: (-0.27, 3.20, 3.40)}
        b = broker_with([days(52)], strikes=strikes)
        spec = sg.build_trade(b, row(), TODAY)
        assert spec["accepted"] is True
        assert (spec["short_strike"], spec["long_strike"], spec["width"]) == (95.0, 94.0, 1.0)
        assert spec["credit"] == 0.40 and spec["min_credit"] == 0.30 and spec["max_loss"] == 0.60

    def test_prefers_5_wide_when_narrower_pairs_also_exist(self):
        strikes = {95: (-0.30, 3.60, 3.80), 92.5: (-0.25, 2.60, 2.80), 90: (-0.22, 2.10, 2.30)}
        b = broker_with([days(52)], strikes=strikes)
        spec = sg.build_trade(b, row(), TODAY)
        assert spec["width"] == 5.0 and spec["long_strike"] == 90.0

    def test_narrow_width_credit_below_scaled_floor_is_rejected(self):
        strikes = {95: (-0.30, 3.60, 3.80), 92.5: (-0.25, 3.10, 3.30)}
        b = broker_with([days(52)], strikes=strikes)
        spec = sg.build_trade(b, row(), TODAY)
        assert spec["accepted"] is False and "0.50 < min 0.75 for $2.5 width" in spec["reason"]

    def test_junk_quotes_are_skipped_not_traded(self):
        # 95's partner 90 has an inflated mid (zero bid, wide ask) -> negative credit; 100/95 is clean.
        strikes = {90: (-0.22, 0.0, 9.00), 95: (-0.30, 3.60, 3.80), 100: (-0.38, 5.40, 5.60)}
        b = broker_with([days(52)], strikes=strikes)
        spec = sg.build_trade(b, row(), TODAY)
        assert spec["accepted"] is True
        assert (spec["short_strike"], spec["long_strike"]) == (100.0, 95.0)

    def test_only_junk_pairs_reports_unusable_quotes(self):
        strikes = {90: (-0.22, 0.0, 9.00), 95: (-0.30, 3.60, 3.80)}
        b = broker_with([days(52)], strikes=strikes)
        spec = sg.build_trade(b, row(), TODAY)
        assert spec["accepted"] is False and "no usable quotes" in spec["reason"]

    def test_no_delta_candidates(self):
        strikes = {95: (-0.10, 3.60, 3.80), 90: (-0.05, 2.10, 2.30)}
        b = broker_with([days(52)], strikes=strikes)
        spec = sg.build_trade(b, row(), TODAY)
        assert spec["accepted"] is False and "delta" in spec["reason"]

    def test_credit_rounds_down_and_rejects_below_150(self):
        b = broker_with([days(52)], long_mid_bump=0.07)
        spec = sg.build_trade(b, row(), TODAY)
        assert spec["credit"] == 1.40
        assert spec["accepted"] is False and "1.40 < min 1.50" in spec["reason"]

    def test_strong_signal_accepts_140(self):
        b = broker_with([days(52)], long_mid_bump=0.07)
        spec = sg.build_trade(b, row(strong=True), TODAY)
        assert spec["credit"] == 1.40 and spec["accepted"] is True

    def test_strong_signal_rejects_135(self):
        b = broker_with([days(52)], long_mid_bump=0.12)
        spec = sg.build_trade(b, row(strong=True), TODAY)
        assert spec["credit"] == 1.35 and spec["accepted"] is False

    def test_out_of_range_expiration_flagged_in_spec(self):
        b = broker_with([days(40)])
        spec = sg.build_trade(b, row(), TODAY)
        assert spec["accepted"] and spec["dte_out_of_range"] is True and spec["dte"] == 40

    def test_no_expiration_skips_before_chain(self):
        b = FakeBroker()
        b.expirations["XYZ"] = [days(33)]
        spec = sg.build_trade(b, row(), TODAY)
        assert spec["accepted"] is False
        assert not any(c[0] == "get_option_chain" for c in b.calls)


def test_nickel_rounding():
    assert sg.round_down_to_nickel(1.49) == 1.45
    assert sg.round_down_to_nickel(1.50) == 1.50
    assert sg.round_down_to_nickel(1.5499) == 1.50
    assert sg.round_to_nickel(0.80) == 0.80
    assert sg.round_to_nickel(0.775) == 0.80
    assert sg.round_to_nickel(0.72) == 0.70
