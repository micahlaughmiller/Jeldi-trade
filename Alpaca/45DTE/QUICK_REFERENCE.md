# Quick Reference - 45-60 DTE Trading System

## Installation (One-Time)
```bash
cd "C:\Users\micah.laughmiller\Documents\Micah's Docs\trading"
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

## Starting the System
```bash
# Continuous mode (recommended)
python run_45dte.py

# Single day test
python scheduler_45dte.py test

# Stop: Press Ctrl+C
```

## Configuration
All settings in `config_45dte.py`:
- API keys (line 7-8)
- Risk parameters (line 14-17)
- RSI thresholds (line 22-25)
- Entry/exit rules (line 27-38)
- Scan time (line 50)

## Daily Workflow

| Time | What Happens |
|------|--------------|
| 9:35 AM ET | System scans S&P 500 for RSI signals |
| 9:35-9:45 | Processes entries, sizes positions |
| Hourly | Checks exits, reduces order prices |
| 4:00 PM ET | System continues monitoring |
| Daily close | Writes summary log |

## Monitoring Logs

**View real-time events:**
```bash
type logs_45dte\session_2025-09-14.log
```

**View daily summary:**
```bash
# Shows tickers analyzed, signals, trades, P&L
type logs_45dte\summary_2025-09-14.json
```

**View trade details:**
```bash
# CSV with entry/exit prices and P&L
type logs_45dte\trades_2025-09-14.csv
```

## Adjusting Parameters

### Entry Criteria
```python
# config_45dte.py

# Make system more selective:
MIN_CREDIT_TARGET = 1.55  # Higher barrier
RSI_OVERSOLD_THRESHOLD = 25  # More oversold

# Make system more aggressive:
MIN_CREDIT_TARGET = 1.35  # Lower barrier
RSI_OVERSOLD_THRESHOLD = 35  # Less oversold
```

### Risk Management
```python
# Reduce risk per trade:
MAX_RISK_PER_TRADE_PCT = 0.02  # 2% instead of 4%

# Stricter portfolio cap:
MAX_PORTFOLIO_RISK_PCT = 0.40  # 40% instead of 52%

# Faster circuit breaker:
MAX_LOSS_HITS_CIRCUIT_BREAKER = 2  # 2 losses instead of 3
```

### Order Management
```python
# Change price reduction speed:
PRICE_REDUCTION_INTERVAL_HOURS = 2  # Every 2 hours
PRICE_REDUCTION_AMOUNT = 0.01  # $0.01 per step

# Change profit target:
PROFIT_TARGET_PCT = 0.40  # 40% instead of 50%
```

## Troubleshooting

| Problem | Solution |
|---------|----------|
| "No module alpaca" | `pip install alpaca-trade-api` |
| "API key not found" | Edit `config_45dte.py` with real keys |
| "No signals generated" | Check RSI thresholds; run `test` mode |
| "Not connecting to Alpaca" | Verify internet; check API status |
| System hangs | Press Ctrl+C; logs are already saved |

## Understanding Logs

### Sample Log Entry
```
[SIGNAL] AAPL: OVERSOLD (RSI14=28.5, RSI28=29.1)
```
= Both RSI indicators < 30, put spread setup

```
[ENTRY_SIGNAL] AAPL Put Credit Spread | Credit: $1.65 | DTE: 52
```
= Generated entry signal, meets all criteria

```
[RISK_CHECK] AAPL: ✓ ALLOWED - Risk check passed
```
= 4% sizing, 52% cap check passed, entering trade

```
[TRADE_OPENED] AAPL put_spread | Qty: 1 | Credit: $1.65
```
= Order placed, tracking with unique ID

```
[TRADE_CLOSED] AAPL | Exit: $0.82 | P&L: $83.00 (50.3%) | Reason: profit_target_hit
```
= Position closed at 50% max gain, $83 profit

## Key Formulas

### Contract Sizing
```
Risk Budget = Account Equity × 4%
Max Loss Per Contract = (Spread Width - Credit) × 100

Contracts = floor(Risk Budget / Max Loss Per Contract)
          = floor($200 / $335) = 1 contract
```

### Portfolio Risk %
```
Total Portfolio Risk = Sum of all position max losses
Portfolio Risk % = Total Risk / Account Equity

If $2,600 risk on $5,000 account:
= $2,600 / $5,000 = 52% (at limit, no new trades)
```

### P&L Calculation
```
Entry Credit: $1.65/share
Exit Price:   $0.82/share
Profit/share: $1.65 - $0.82 = $0.83

Per Contract: $0.83 × 100 = $83
For 1 contract: $83 total

P&L %: ($0.83 / $1.65) × 100 = 50.3%
```

## Common Scenarios

### "I want tighter position management"
```python
# In config_45dte.py:
PRICE_REDUCTION_INTERVAL_HOURS = 0.5  # Every 30 min
PROFIT_TARGET_PCT = 0.40  # Close at 40% instead of 50%
```

### "I want to skip certain tickers"
Edit `market_data_handler.py`:
```python
SP500_TICKERS = [
    # Remove any you want to skip
    # "AAPL",  # Commented out = skipped
    "MSFT", "NVDA", ...
]
```

### "I want manual approval before entries"
Modify `scheduler_45dte.py`:
```python
# Before placing trade, add:
input(f"Enter trade {symbol}? (y/n): ")
```

### "I want to limit number of daily trades"
Add to `config_45dte.py`:
```python
MAX_TRADES_PER_DAY = 5
```

Then in `scheduler_45dte.py`, count trades before entry.

## System Health Checks

**Every morning before market:**
```bash
# 1. Verify logs from yesterday exist
ls logs_45dte/summary_*.json

# 2. Check yesterday's P&L
type logs_45dte/summary_2025-09-13.json

# 3. Confirm API keys in config
type config_45dte.py | grep ALPACA

# 4. Run test to confirm setup
python scheduler_45dte.py test
```

## When to Scale to Live Trading

✅ 2+ weeks consistent paper trading  
✅ Win rate ≥ 60%  
✅ Circuit breaker never triggered  
✅ Daily losses < 3%  
✅ Understand every log message  
✅ Have manual override plan  

Only then:
```python
# config_45dte.py - CHANGE THIS TO GO LIVE:
PAPER_TRADING = False  # ⚠️ REAL MONEY NOW
```

## Emergency Stop

If system misbehaves:
```bash
# Press Ctrl+C to stop gracefully
# Logs and position management continue

# If completely stuck:
# Kill the terminal window
# System will have logged all positions
```

## Need Help?

1. Check logs: `logs_45dte/session_*.log`
2. Read SYSTEM_OVERVIEW.md for detailed flow
3. Review SETUP_GUIDE.md for installation
4. Read README_45DTE.md for full documentation

---

**Status**: Paper Trading Active ✓  
**Last Updated**: 2025-09-14  
**Version**: 1.0.0
