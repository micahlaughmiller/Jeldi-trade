# ============================================
# MARKET DATA
# Fetches ES/SPX bars for ORB calculation
# ============================================

import yfinance as yf
import pandas as pd
import logging

logger = logging.getLogger(__name__)


def get_intraday(symbol, period="1d", interval="1m"):
    """
    Get intraday bars from yfinance
    
    Args:
        symbol: 'ES=F' for ES futures, '^GSPC' for SPX
        period: '1d', '5d', etc
        interval: '1m', '5m', '15m', etc
    
    Returns:
        DataFrame with OHLCV
    """
    try:
        df = yf.download(
            symbol,
            period=period,
            interval=interval,
            auto_adjust=False,
            progress=False,
        )
        
        if df.empty:
            logger.warning(f"No data for {symbol}")
            return df
        
        # Normalize column names
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0].lower() for c in df.columns]
        else:
            df.columns = [str(c).lower() for c in df.columns]
        
        return df.dropna()
    
    except Exception as e:
        logger.error(f"Error fetching {symbol}: {e}")
        return pd.DataFrame()


def get_overnight_levels(symbol='ES=F'):
    """
    Get yesterday's high/low (overnight levels)
    
    Returns:
        {'high': float, 'low': float}
    """
    try:
        df = get_intraday(symbol, period="5d", interval="1d")
        
        if df.empty or len(df) < 2:
            return None
        
        # Last row is yesterday
        yesterday = df.iloc[-2]
        
        return {
            'high': float(yesterday['high']),
            'low': float(yesterday['low']),
        }
    
    except Exception as e:
        logger.error(f"Error getting overnight levels: {e}")
        return None


def get_opening_range(symbol='ES=F', minutes=15):
    """
    Calculate opening range (first N minutes of today)
    
    Args:
        symbol: 'ES=F' for ES futures
        minutes: minutes to include (default 15)
    
    Returns:
        {'high': float, 'low': float}
    """
    try:
        df = get_intraday(symbol, period="1d", interval="1m")
        
        if df.empty or len(df) < minutes:
            return None
        
        # Take first N bars (9:30-9:45 ET)
        or_bars = df.iloc[:minutes]
        
        return {
            'high': float(or_bars['high'].max()),
            'low': float(or_bars['low'].min()),
        }
    
    except Exception as e:
        logger.error(f"Error calculating ORB: {e}")
        return None


def get_current_price(ticker):
    try:
        # Assuming you download minute data using yf.Ticker or yf.download
        data = yf.download(ticker, period="1d", interval="1m", progress=False)
        
        if data.empty:
            return None
            
        # FIX: Access the last price and convert to scalar using .iloc[-1]
        latest_price = data['Close'].iloc[-1]
        
        # If latest_price is still a 1-element Series (multi-index DataFrame)
        if hasattr(latest_price, 'item'):
            return float(latest_price.item())
            
        return float(latest_price)
        
    except Exception as e:
        logger.error(f"Error getting price for {ticker}: {e}")
        return None