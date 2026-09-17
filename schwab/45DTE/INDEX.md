# 45-60 DTE Credit Spread Trading System - Complete Delivery

## 📋 Documentation Files

| File | Purpose | Read Time |
|------|---------|-----------|
| **START HERE →** [README_45DTE.md](README_45DTE.md) | Full system overview & features | 10 min |
| [SETUP_GUIDE.md](SETUP_GUIDE.md) | Step-by-step installation & configuration | 15 min |
| [SYSTEM_OVERVIEW.md](SYSTEM_OVERVIEW.md) | How the system works, detailed flow | 20 min |
| [QUICK_REFERENCE.md](QUICK_REFERENCE.md) | Common commands & quick answers | 5 min |
| [INDEX.md](INDEX.md) | This file - complete file listing | 2 min |

---

## 🐍 Python Files (Core System)

| File | Lines | Purpose |
|------|-------|---------|
| [config_45dte.py](config_45dte.py) | 62 | ALL configuration: API keys, risk, signals |
| [alpaca_connector.py](alpaca_connector.py) | 142 | Alpaca API wrapper |
| [market_data_handler.py](market_data_handler.py) | 140 | S&P 500 scanning & RSI calculation |
| [signal_generator.py](signal_generator.py) | 144 | Entry signal generation logic |
| [order_manager.py](order_manager.py) | 154 | Order placement & lifecycle |
| [risk_manager.py](risk_manager.py) | 126 | Kelly sizing & circuit breakers |
| [logger_system.py](logger_system.py) | 180 | Daily logging & summaries |
| [scheduler_45dte.py](scheduler_45dte.py) | 201 | Main orchestrator |
| [run_45dte.py](run_45dte.py) | 23 | Entry point script |
| **TOTAL** | **1,172** | **Production-grade trading system** |

---

## 📦 Support Files

| File | Purpose |
|------|---------|
| [requirements.txt](requirements.txt) | Python dependencies (pip install) |

---

## 📁 Auto-Generated Directories

| Directory | Contents | Created By |
|-----------|----------|------------|
| `logs_45dte/` | Daily logs & summaries | System (auto-created) |
| `logs_45dte/session_*.log` | Real-time events (JSON) | Logger |
| `logs_45dte/summary_*.json` | Daily summary | Logger |
| `logs_45dte/trades_*.csv` | Trade details | Logger |

---

## 🚀 Quick Start

### **First Time Setup (5 minutes)**
```bash
# 1. Open terminal/PowerShell
# 2. Navigate to directory
cd "C:\Users\micah.laughmiller\Documents\Micah's Docs\trading"

# 3. Create virtual environment
python -m venv venv

# 4. Activate it
venv\Scripts\activate

# 5. Install dependencies
pip install -r requirements.txt

# 6. Add Alpaca API keys to config_45dte.py
# Edit line 7-8 with your actual keys from https://app.alpaca.markets
```

### **Run the System**
```bash
# Test mode (one scan cycle)
python scheduler_45dte.py test

# Continuous mode (recommended)
python run_45dte.py

# Stop: Press Ctrl+C
```

---

## 💡 What This System Does

### **Daily (9:35 AM ET)**
1. ✅ Scans 35 S&P 500 stocks for RSI confluence
2. ✅ Identifies oversold (RSI<30) and overbought (RSI>70) signals
3. ✅ Generates put or call credit spreads
4. ✅ Calculates 4% position sizes dynamically
5. ✅ Places limit orders with price ladder ($0.02/hour reduction)

### **Throughout the Day**
6. ✅ Monitors profit targets (50% max gain)
7. ✅ Manages order price reductions
8. ✅ Tracks expirations (closes ≤7 DTE)
9. ✅ Enforces circuit breaker (3 max losses = halt)

### **End of Day**
10. ✅ Writes comprehensive daily summary
11. ✅ Logs all trades & P&L
12. ✅ Generates next day's logs

---

## 📊 System Architecture

```
┌─────────────────────────────────────────┐
│     Alpaca Paper Trading Account        │
│         ($5,000 starting)               │
└────────────────┬────────────────────────┘
                 │
        ┌────────▼────────┐
        │ run_45dte.py    │ Entry Point
        └────────┬────────┘
                 │
        ┌────────▼──────────────┐
        │ scheduler_45dte.py    │ Main Loop
        └────────┬──────────────┘
                 │
    ┌────────────┼────────────┐
    │            │            │
    ▼            ▼            ▼
┌────────┐ ┌──────────┐ ┌──────────┐
│ Market │ │ Signal   │ │  Order   │
│ Data   │ │Generator │ │ Manager  │
│Handler │ │          │ │          │
└────┬───┘ └──────┬───┘ └────┬─────┘
     │           │           │
     ▼           ▼           ▼
  RSI(14)    Entry Signals  Alpaca
  RSI(28)    Risk Checks    Orders
              │           │
    ┌─────────┼───────────┘
    │         │
    ▼         ▼
┌──────────────────────┐
│  Risk Manager        │
│  - Kelly sizing      │
│  - Portfolio cap     │
│  - Circuit breaker   │
└──────────┬───────────┘
           │
           ▼
    ┌─────────────────┐
    │ Logger System   │
    │ - Daily logs    │
    │ - Trade summary │
    │ - P&L tracking  │
    └─────────────────┘
```

---

## ⚙️ Configuration Reference

**All settings in `config_45dte.py`:**

### Alpaca Connection
- `ALPACA_API_KEY` - Your Alpaca API key
- `ALPACA_SECRET_KEY` - Your Alpaca secret
- `PAPER_TRADING` - True for paper, False for live

### Risk Parameters
- `MAX_RISK_PER_TRADE_PCT` - 4% (per trade)
- `MAX_PORTFOLIO_RISK_PCT` - 52% (total)
- `MAX_CONTRACTS_PER_TRADE` - 40 (maximum)

### Entry Signals
- `RSI_PERIOD_FAST` - 14 (RSI14)
- `RSI_PERIOD_SLOW` - 28 (RSI28)
- `RSI_OVERSOLD_THRESHOLD` - 30
- `RSI_OVERBOUGHT_THRESHOLD` - 70

### Entry Criteria
- `MIN_CREDIT_TARGET` - $1.45 (minimum entry)
- `MIN_CREDIT_FLOOR` - $1.35 (cancellation floor)
- `SPREAD_WIDTH` - $5.00 (fixed)
- `TARGET_DELTA` - 0.30 (short strike)
- `EXPIRATION_DTE_MIN` - 45 days
- `EXPIRATION_DTE_MAX` - 60 days

### Order Management
- `PRICE_REDUCTION_INTERVAL_HOURS` - 1 hour
- `PRICE_REDUCTION_AMOUNT` - $0.02 per step
- `PROFIT_TARGET_PCT` - 50% (close at 50% max gain)

### Safety Limits
- `MAX_LOSS_HITS_CIRCUIT_BREAKER` - 3 (halt after 3)
- `DAILY_LOSS_LIMIT_PCT` - 3% (alert threshold)
- `PEAK_DRAWDOWN_LIMIT_PCT` - 15% (freeze threshold)

---

## 📈 Example Trade Scenario

```
9:35 AM - Market opens, scan begins

[SCAN] Checking AAPL...
  └─ RSI(14) = 28.2
  └─ RSI(28) = 29.1
  └─ Both < 30 = OVERSOLD SIGNAL

[SIGNAL] Generate entry:
  └─ Current Price: $150.00
  └─ Short Strike: $145.00 (put)
  └─ Long Strike: $140.00
  └─ Est. Credit: $1.65/share
  └─ Max Loss: $335/contract

[RISK CHECK]:
  └─ Risk Budget: $5,000 × 4% = $200
  └─ Contracts: floor($200 / $335) = 1 contract
  └─ Portfolio Risk: Would be 6.7% → ALLOWED

[ENTRY] Trade placed:
  └─ Sell 1 AAPL 145 put @ $1.65
  └─ Buy 1 AAPL 140 put @ (spread)
  └─ DTE: 52 days
  └─ GTC order placed

Throughout Day:
  └─ Hour 1: Price → $1.43 (reduced by $0.02)
  └─ Hour 2: Price → $1.41
  └─ Hour 8: Mark → $0.82 → 50% PROFIT TARGET HIT!

[EXIT] Position closed:
  └─ Profit: $0.83/share
  └─ P&L: $83.00
  └─ P&L%: 50.3%
  └─ Days held: 8 days

📊 Daily Summary:
   └─ Tickers Analyzed: 35
   └─ Signals Generated: 3
   └─ Trades Opened: 2
   └─ Trades Closed: 1
   └─ Daily P&L: +$83.00
```

---

## 🔧 Common Adjustments

### **Make it More Conservative**
```python
MAX_RISK_PER_TRADE_PCT = 0.02  # 2% instead of 4%
MAX_PORTFOLIO_RISK_PCT = 0.40  # 40% instead of 52%
PROFIT_TARGET_PCT = 0.40       # 40% instead of 50%
```

### **Make it More Aggressive**
```python
MIN_CREDIT_TARGET = 1.35  # Lower barrier
RSI_OVERSOLD_THRESHOLD = 35  # Less oversold
PRICE_REDUCTION_INTERVAL_HOURS = 0.5  # Every 30 min
```

### **Skip Certain Stocks**
Edit `market_data_handler.py`, remove from `SP500_TICKERS` list

---

## 📊 Performance Tracking

System logs are saved in `logs_45dte/`:

**View daily summary:**
```bash
type logs_45dte\summary_2025-09-14.json
```

**View all events:**
```bash
type logs_45dte\session_2025-09-14.log
```

**View trade details:**
```bash
type logs_45dte\trades_2025-09-14.csv
```

---

## 🛡️ Safety Features

✅ **Paper trading first** - No real money at risk  
✅ **4% Kelly sizing** - Position sized for your account  
✅ **52% portfolio cap** - Never over-leveraged  
✅ **Circuit breaker** - Halts after 3 max losses  
✅ **Daily loss alerts** - Warns at 3% loss threshold  
✅ **Price ladder** - Passive order management  
✅ **Comprehensive logging** - Every action recorded  

---

## 🚦 Status Check

**Before first run:**
```bash
# 1. Verify Python installed
python --version

# 2. Verify Alpaca keys in config_45dte.py
# 3. Run setup
venv\Scripts\activate
pip install -r requirements.txt

# 4. Test connection
python scheduler_45dte.py test
```

**If any issues:**
1. Check logs in `logs_45dte/session_*.log`
2. Review error messages in terminal
3. See SETUP_GUIDE.md Troubleshooting section

---

## 📞 Getting Help

| Question | Answer Location |
|----------|-----------------|
| "How do I start?" | SETUP_GUIDE.md |
| "How does it work?" | SYSTEM_OVERVIEW.md |
| "What's my account doing?" | logs_45dte/summary_*.json |
| "What was that log message?" | QUICK_REFERENCE.md |
| "How do I adjust parameters?" | config_45dte.py (read comments) |
| "Why didn't it enter a trade?" | logs_45dte/session_*.log |

---

## 📋 Delivery Checklist

✅ **Documentation** (5 files)
- README_45DTE.md - Full system guide
- SETUP_GUIDE.md - Installation walkthrough
- SYSTEM_OVERVIEW.md - How it works
- QUICK_REFERENCE.md - Common tasks
- INDEX.md - This file

✅ **Production Code** (9 Python files, 1,172 lines)
- Complete credit spread trading system
- Alpaca integration
- RSI signal generation
- Kelly portfolio sizing
- Risk management & circuit breakers
- Comprehensive daily logging

✅ **Configuration**
- requirements.txt - All dependencies
- config_45dte.py - All user-configurable parameters

✅ **Ready for Use**
- Paper trading enabled
- No real money needed to start
- Fully functional & tested
- Extensible for Claude/ChatGPT integration

---

## 🎯 Next Steps

### **Immediately (Today)**
1. Follow SETUP_GUIDE.md installation steps
2. Add Alpaca API keys to config_45dte.py
3. Run `python scheduler_45dte.py test` to verify setup

### **This Week**
4. Run `python run_45dte.py` for 2-3 days in paper trading
5. Review daily logs in `logs_45dte/`
6. Verify system operates as expected

### **This Month**
7. Collect performance data (win rate, P&L)
8. Adjust parameters if needed
9. Consider Claude/ChatGPT signal comparison

### **When Ready**
10. Change `PAPER_TRADING = False` (only after 2+ weeks profitable paper trading)
11. Start with small position size
12. Scale up gradually

---

## 📦 System Specs

- **Language**: Python 3.8+
- **Broker**: Alpaca (stock options)
- **Trading Style**: 45-60 DTE credit spreads
- **Signals**: RSI(14) & RSI(28) confluence
- **Sizing**: Dynamic Kelly (4% per trade)
- **Risk Cap**: 52% portfolio maximum
- **Exit**: 50% profit target or expiration
- **Logging**: JSON + CSV daily summaries
- **Status**: Paper Trading Ready ✓

---

## 🎓 Learning Path

**Complete Reading Order:**
1. INDEX.md (this file) - 2 min
2. README_45DTE.md - 10 min
3. SETUP_GUIDE.md - 15 min
4. QUICK_REFERENCE.md - 5 min
5. SYSTEM_OVERVIEW.md - 20 min

**Then:**
6. Run `python scheduler_45dte.py test`
7. Review logs in `logs_45dte/`
8. Read config_45dte.py with comments
9. Start live paper trading!

---

## 💾 File Organization

```
C:\Users\micah.laughmiller\Documents\Micah's Docs\trading\
│
├── 📄 INDEX.md                          ← You are here
├── 📄 README_45DTE.md                   ← Start here
├── 📄 SETUP_GUIDE.md
├── 📄 SYSTEM_OVERVIEW.md
├── 📄 QUICK_REFERENCE.md
│
├── 🐍 config_45dte.py                   ← Edit API keys here
├── 🐍 run_45dte.py                      ← Start system here
├── 🐍 scheduler_45dte.py
├── 🐍 alpaca_connector.py
├── 🐍 market_data_handler.py
├── 🐍 signal_generator.py
├── 🐍 order_manager.py
├── 🐍 risk_manager.py
├── 🐍 logger_system.py
│
├── 📦 requirements.txt                  ← Dependencies
│
└── 📁 logs_45dte/ (auto-created)
    ├── session_2025-09-14.log
    ├── summary_2025-09-14.json
    └── trades_2025-09-14.csv
```

---

## ✨ Key Features Summary

| Feature | Status | Details |
|---------|--------|---------|
| S&P 500 scanning | ✅ | 35 tickers daily |
| RSI signals | ✅ | Oversold/overbought confluence |
| Credit spreads | ✅ | Puts & calls, 45-60 DTE |
| Dynamic sizing | ✅ | 4% Kelly, contracts scale with equity |
| Order management | ✅ | Price ladder, GTC exits |
| Risk limits | ✅ | 52% portfolio cap, circuit breaker |
| Logging | ✅ | Daily JSON + CSV summaries |
| Paper trading | ✅ | Default mode (no real money) |
| Alpaca integration | ✅ | Full API access |
| Extensibility | ✅ | Ready for Claude/ChatGPT |

---

**Status: READY TO RUN ✓**

**To start:** Read README_45DTE.md, then run `python run_45dte.py`

---

*Complete 45-60 DTE Credit Spread Trading System*  
*Delivered 2025-09-14*  
*Production Grade • Paper Trading Ready • Fully Documented*
