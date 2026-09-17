# ============================================
# TIER SCALING TESTS
# Verify account tier system works correctly
# ============================================

from risk_manager import RiskManager


def check_tier(equity, expected_width, expected_max):
    """Test a specific equity level"""
    
    risk = RiskManager()
    risk.reset_day(equity)
    tier = risk.tier()
    
    assert tier.spread_width == expected_width, \
        f"Width mismatch at ${equity}: expected {expected_width}, got {tier.spread_width}"
    
    assert tier.max_contracts == expected_max, \
        f"Max contracts mismatch at ${equity}: expected {expected_max}, got {tier.max_contracts}"
    
    print(
        f"PASS | "
        f"${equity:,.2f} | "
        f"{tier.name} | "
        f"{tier.spread_width} point | "
        f"max {tier.max_contracts}"
    )


# Run all tier tests
check_tier(2_000, 5, 1)
check_tier(4_999.99, 5, 1)
check_tier(5_000, 5, 2)
check_tier(9_999.99, 5, 2)
check_tier(10_000, 10, 5)
check_tier(24_999.99, 10, 5)
check_tier(25_000, 10, 10)
check_tier(49_999.99, 10, 10)
check_tier(50_000, 10, None)
check_tier(100_000, 10, None)

print()
print("✅ ALL TIER TESTS PASSED")