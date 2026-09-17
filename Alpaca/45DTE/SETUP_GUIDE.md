# 45-60 DTE Credit Spread System - Setup Guide

## Prerequisites
- Python 3.8+ installed
- Alpaca account (free, paper trading available)
- ~30 minutes for setup

## Step 1: Install Python Dependencies

### On Windows:
```bash
cd C:\Users\micah.laughmiller\Documents\Micah's Docs\trading

# Create virtual environment (recommended)
python -m venv venv
venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### On Mac/Linux:
```bash
cd ~/Documents/Micah\'s\ Docs/trading

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

## Step 2: Get Alpaca API Keys

1. Visit **https://app.alpaca.markets**
2. Create account (or login if you have one)
3. Go to **Account Settings → API Keys**
4. Copy your **API Key** and **Secret Key**

## Step 3: Configure Your API Keys

Edit `config_45dte.py`:

```python
ALPACA_API_KEY = "PKxxxxxxxxxxxxxx"
ALPACA_SECRET_KEY = "xxxxxxxxxxxxxxxxxxxxxxxxxxxx"
```

⚠️ **Important**: Never commit API keys to version control. Keep them private!

## Step 4: Test Connection

Run a quick test:

```bash
python scheduler_45dte.py test
```

Expected output:
```
======================================================================
45-60 DTE CREDIT SPREAD TRADING SYSTEM
======================================================================
Paper Trading: True
Scan Time: 09:35 ET
Log Location: ./logs_45dte
======================================================================

[SYSTEM_START] Scheduler initialized and ready
[SCAN_START] Beginning scan of 35 S&P 500 stocks
...
[SCAN_COMPLETE] Scan complete. Found X signals.

======================================================================
DAILY SUMMARY - 2025-09-14
======================================================================
Tickers Analyzed: 35
Signals Generated: X
Trades Opened: X
Trades Closed: X
Daily P&L: $0.00
Log Location: ./logs_45dte/summary_2025-09-14.json
======================================================================
```

## Step 5: Run the System

### Paper Trading (Recommended to start):
```bash
python run_45dte.py
```

This will:
1. Scan S&P 500 at 9:35 AM ET daily
2. Generate entry signals based on RSI confluence
3. Place orders and manage exits
4. Log all activity

**Press Ctrl+C to stop**

### Single Day Test:
```bash
python scheduler_45dte.py test
```

This runs one scan cycle for testing purposes.

## Step 6: Monitor Logs

Logs are saved in `logs_45dte/` directory:

```
logs_45dte/
├── session_2025-09-14.log          # Real-time events
├── summary_2025-09-14.json         # Daily summary
└── trades_2025-09-14.csv           # Trade details
```

### View daily summary:
```bash
# Windows
type logs_45dte\summary_2025-09-14.json

# Mac/Linux
cat logs_45dte/summary_2025-09-14.json
```

## Configuration Reference

### Entry Signals
- **RSI(14) < 30 AND RSI(28) < 30** → Put Credit Spread (sell puts)
- **RSI(14) > 70 AND RSI(28) > 70** → Call Credit Spread (sell calls)
- **Min Credit**: $1.45 per share
- **Max Credit Reduction**: $0.02/hour down to $1.35 floor

### Risk Management
- **Per Trade Risk**: 4% of account equity
- **Portfolio Cap**: 52% maximum
- **Circuit Breaker**: 3+ trades hitting max loss = halt entries
- **Daily Loss Alert**: -3% triggers warning

### Exit Conditions
1. 50% profit target hit (GTC order)
2. Price ladder reduces limit price
3. Expiration approaches (≤7 DTE)
4. Order price falls below $1.35

## Troubleshooting

### "ModuleNotFoundError: No module named 'alpaca'"
```bash
pip install alpaca-trade-api
```

### "ALPACA_API_KEY not set"
Edit `config_45dte.py` with your actual API keys.

### "No signals generated"
- Check RSI thresholds (oversold <30, overbought >70)
- Verify 6-month data is available
- Check market conditions during low-volatility periods

### "Equity is $0 or negative"
- Verify Alpaca account has starting balance
- Check account funding

### Port/Connection Issues
- Confirm Alpaca API status: https://status.alpaca.markets/
- Verify internet connection
- Check firewall settings

## Next Steps

### 1. Paper Trade for 1-2 Weeks
- Verify system operates as expected
- Check logs daily
- Monitor P&L and win rate

### 2. Collect Performance Data
- Run backtests on historical data
- Analyze which tickers perform best
- Calculate Sharpe ratio, max drawdown

### 3. Consider Signal Improvements
- Integrate Claude or ChatGPT for signal confirmation
- Add technical filters (MACD, moving averages)
- Test different RSI thresholds

### 4. When Ready for Live Trading
Change in `config_45dte.py`:
```python
PAPER_TRADING = False  # ⚠️ REAL MONEY
```

⚠️ **Only switch to live after:**
- 2+ weeks of paper trading
- Consistent profitability
- Deep understanding of the system
- Risk management guardrails tested

## System Architecture

```
Alpaca API
    ↓
alpaca_connector.py (Connect to broker)
    ↓
market_data_handler.py (Fetch S&P 500, calculate RSI)
    ↓
signal_generator.py (Generate entry signals)
    ↓
risk_manager.py (Kelly sizing, portfolio caps)
    ↓
order_manager.py (Place/manage orders)
    ↓
logger_system.py (Daily logs & summaries)
    ↓
scheduler_45dte.py (Orchestrate everything)
    ↓
run_45dte.py (Main entry point)
```

## Support & Debugging

**For detailed logs:**
Check `logs_45dte/session_YYYY-MM-DD.log` for real-time events.

**For system issues:**
1. Run `python scheduler_45dte.py test` to verify setup
2. Check Alpaca account: https://app.alpaca.markets/
3. Review error messages in terminal

**For signal questions:**
Review `logs_45dte/summary_YYYY-MM-DD.json` for:
- Which tickers were scanned
- Which passed RSI filters
- Why entries were rejected

---

**Ready to start?**
```bash
python run_45dte.py
```

Good luck! 📈
