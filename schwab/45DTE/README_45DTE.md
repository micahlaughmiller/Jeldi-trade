# 45-60 DTE Credit Spread Trading System

## Overview
Automated trading system for S&P 500 credit spreads using RSI(14) and RSI(28) confluence signals. Paper trading first via Alpaca API.

## Features
- ✅ S&P 500 daily scan for RSI overbought/oversold confluence
- ✅ Automatic credit spread entry (call spread if overbought, put spread if oversold)
- ✅ Dynamic contract sizing: 4% risk per trade, 52% portfolio max
- ✅ Order price ladder: $0.02 reduction every hour, floor at $1.35
- ✅ Automatic exits: 50% profit target (GTC) + expiration management
- ✅ Circuit breaker: Halt entries if 3+ trades hit max loss
- ✅ Comprehensive daily logging: tickers analyzed, trades, P&L
- ✅ Alpaca paper trading integration
- ✅ Modular architecture for Claude/ChatGPT signal comparison

## System Architecture

```
├── config_45dte.py              # All parameters & risk limits
├── alpaca_connector.py          # Alpaca API wrapper
├── market_data_handler.py       # S&P 500 scan, RSI calculation
├── signal_generator.py          # Entry signal logic
├── order_manager.py             # Order lifecycle management
├── risk_manager.py              # Kelly sizing, circuit breakers
├── logger_system.py             # Daily logging & summaries
├── scheduler_45dte.py           # Main orchestrator
├── run_45dte.py                 # Entry point
└── logs_45dte/                  # Daily logs & summaries
    ├── session_YYYY-MM-DD.log
    ├── summary_YYYY-MM-DD.json
    └── trades_YYYY-MM-DD.csv
```

## Quick Start

### 1. Install Dependencies
```bash
pip install alpaca-trade-api pandas numpy yfinance
```

### 2. Configure Alpaca API Keys
Edit `config_45dte.py`:
```python
ALPACA_API_KEY = "your_api_key_here"
ALPACA_SECRET_KEY = "your_secret_key_here"
```

Get keys from: https://app.alpaca.markets/account/settings/keys

### 3. Run the System

**Test mode (single day backtest):**
```bash
python scheduler_45dte.py test
```

**Live mode (continuous):**
```bash
python run_45dte.py
```

## Strategy Parameters

### Entry Signals
- **Universe**: S&P 500 stocks
- **Timeframe**: 6-month daily candles
- **Indicators**: RSI(14) and RSI(28) must both agree
- **Oversold Entry**: RSI(14) < 30 AND RSI(28) < 30 → **PUT CREDIT SPREAD**
- **Overbought Entry**: RSI(14) > 70 AND RSI(28) > 70 → **CALL CREDIT SPREAD**

### Entry Criteria
- Short strike at .30 delta
- $5.00 spread width
- Credit ≥ $1.45 per share ($150/contract)
- Expiration: 45-60 DTE
- Max loss per contract: (Spread Width - Credit) × 100

### Exit Criteria
1. **Profit Target**: Close at 50% of max gain → GTC order
2. **Price Ladder**: Every 1 hour, reduce limit by $0.02
3. **Floor Price**: Cancel if credit falls below $1.35
4. **Expiration**: Close if ≤7 DTE
5. **Max Loss**: Close if cost to close ≥ max loss

### Risk Management
- **Per Trade Risk**: 4% of account equity
- **Portfolio Cap**: 52% maximum at-risk
- **Contract Sizing**: `floor(account_equity × 0.04 / max_loss_per_contract)`
- **Max Contracts**: 40 per trade
- **Circuit Breaker**: Halt new entries if 3+ trades hit max loss
- **Daily Loss Alert**: If -3% daily P&L
- **Peak Drawdown Freeze**: If -15% from peak

## Log Files

### session_YYYY-MM-DD.log
Real-time JSON event log:
```json
{"timestamp": "2025-09-14T09:35:42.123", "type": "SIGNAL", "message": "..."}
```

### summary_YYYY-MM-DD.json
Daily summary with:
- Tickers analyzed
- Signals generated
- Trades opened/closed
- Daily P&L
- All events

### trades_YYYY-MM-DD.csv
CSV of all trades:
```
symbol,entry_price,exit_price,profit_per_contract,total_pnl,pnl_pct,...
```

## Alpaca vs Production

### Paper Trading (Current)
✓ No real money at risk  
✓ Full API access  
✗ No real options chain (simulated)  
✗ No real fills (assumed execution)  

### Production (Future)
When ready to trade real money:
1. Change `PAPER_TRADING = False` in config
2. Integrate real options chain API (Polygon.io, etc.)
3. Add position monitoring from broker
4. Implement webhook alerts
5. Add human approval gates

## Extending for Claude/ChatGPT

To compare signal generators:

1. **Create signal_generator_claude.py**
   ```python
   class SignalGeneratorClaude(SignalGenerator):
       def generate_entry_signal(self, rsi_signal, ...):
           # Call Claude API for signal generation
           pass
   ```

2. **Modify config to select generator:**
   ```python
   SIGNAL_GENERATOR = "claude"  # or "astra", "rsi" (default)
   ```

3. **Update scheduler to use selected generator**

## Troubleshooting

### No signals generated?
- Check RSI calculations: `print(data['rsi_14'])` and `data['rsi_28']`
- Verify RSI thresholds: oversold<30, overbought>70
- Check 6-month data available

### Orders not placing?
- Verify Alpaca API keys
- Check account has buying power
- Confirm paper trading enabled

### Low credit signals rejected?
- Adjust `MIN_CREDIT_TARGET` in config
- Check market volatility (lower IV = lower credit)
- Try different timeframe or expiration

## Future Roadmap

- [ ] Integration with Claude Fable for signal comparison
- [ ] Integration with ChatGPT ASTRA for signal comparison  
- [ ] Real options chain API integration (Polygon.io)
- [ ] Position monitoring & Greeks tracking
- [ ] Webhook alerts (Discord, Slack)
- [ ] Backtesting framework with walk-forward analysis
- [ ] Equity curve visualization
- [ ] Performance dashboard

## Support

For issues, check:
1. Logs in `logs_45dte/` directory
2. Console output for errors
3. Alpaca API status: https://status.alpaca.markets/

---

**System Status**: Paper Trading Ready ✓  
**Last Updated**: 2025-09-14
