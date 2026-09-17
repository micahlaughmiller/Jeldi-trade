"""Market data handler for S&P 500 stock scanning and RSI calculation."""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import yfinance as yf
from alpaca_connector import AlpacaConnector
from config_45dte import (
    RSI_PERIOD_FAST,
    RSI_PERIOD_SLOW,
    RSI_OVERSOLD_THRESHOLD,
    RSI_OVERBOUGHT_THRESHOLD,
    DATA_LOOKBACK_MONTHS,
)

# Complete S&P 500 stock list (all 500+ stocks)
SP500_TICKERS = [
    # Top 50
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "BRK.B", "JNJ", "WMT",
    "JPM", "V", "PG", "UNH", "HD", "MA", "DIS", "NFLX", "ADBE", "CRM",
    "ABT", "XOM", "NKE", "INTC", "AMD", "BA", "CSCO", "KO", "MCD", "PEP",
    "IBM", "QCOM", "LLY", "COST", "MRK", "AVGO", "ACN", "HON", "AXP", "CMCSA",
    "AMGN", "SBUX", "BLK", "MDLZ", "RTX", "TXN", "PM", "LOW", "GE", "CAT",
    # 51-100
    "CVX", "SO", "NEE", "T", "AMAT", "LRCX", "PYPL", "MNST", "GS", "EXC",
    "ORLY", "AEP", "COP", "PLD", "DOW", "DUK", "FDX", "REGN", "ZM", "SNPS",
    "MU", "CDNS", "VRTX", "BKNG", "CME", "GILD", "SYK", "PCAR", "KMI", "DASH",
    "COIN", "ROST", "TROW", "YUM", "DECK", "CTVA", "ALGN", "APTV", "PARA", "RCL",
    "MAR", "IQV", "ZTS", "HUM", "CI", "AZO", "PAYX", "TJX", "MCHP", "EL",
    # 101-150
    "PH", "OKE", "JKHY", "ROK", "HPE", "MCK", "ULTA", "APP", "VRSK", "TTWO",
    "CTAS", "DLTR", "ROP", "FIS", "EXPD", "MTCH", "TT", "PTC", "SWKS", "CHTR",
    "RPAY", "KKR", "DHI", "ODFL", "EMR", "TEL", "WTW", "ACGL", "WRB", "IDXX",
    "AEE", "CPRT", "PEG", "JBHT", "ETSY", "PODD", "TRMB", "ENPH", "UPS", "SLB",
    "IRM", "BIIB", "DNOW", "RMD", "FTNT", "XEL", "FFIV", "CRL", "FORM", "AIG",
    # 151-200
    "PNR", "INTU", "UDR", "ARE", "SJM", "WST", "BDX", "SNA", "FRT", "STWD",
    "TAP", "MKTX", "SPG", "BBWI", "RPM", "EFX", "BLDR", "CLF", "AES", "LVS",
    "DAL", "FOXA", "FOX", "LYV", "MAS", "FSLR", "KEYS", "HLI", "CAH", "PWR",
    "TIGO", "KNSL", "RES", "MORN", "ALLY", "FITB", "NDSN", "DELL", "NTAP", "AFG",
    "NXPI", "SWK", "MRNA", "AIZ", "WLK", "VICI", "LMNX", "PKG", "PHM", "RRGB",
    # 201-250
    "CASY", "CLX", "CBOE", "CHRW", "APOG", "MOH", "VSH", "FRPT", "TOST", "VOYA",
    "TFX", "ERIE", "IEMG", "SCL", "CC", "QFIN", "RLI", "VSAT", "UGI", "LKQ",
    "OC", "TXRH", "UMC", "UFPI", "PSA", "GWW", "UNM", "IEX", "THO", "WDAY",
    "OWL", "QLYS", "NEGG", "SFM", "UTL", "VMC", "CNA", "AAL", "LW", "MXL",
    "VRSN", "MOS", "CACC", "TOL", "OGE", "SQM", "HSY", "AAP", "TYL", "NRG",
    # 251-300
    "INGR", "AMCX", "TTD", "RNG", "DRI", "TNC", "HWKN", "ACM", "VCIT", "BAP",
    "FAF", "RY", "LH", "SUI", "VST", "PAG", "HST", "KMX", "HLT", "BKR",
    "PLNT", "APEI", "OMF", "UHS", "POOL", "JMIA", "XPO", "INSP", "VCTR", "SCKT",
    "KHC", "CPT", "WTS", "IFF", "LHCG", "BFAM", "GNTX", "SIR", "BHC", "PKE",
    "DXC", "BWA", "MLI", "CAG", "SKT", "RCMT", "SEM", "PRGO", "BXP", "MLM",
    # 301-350
    "VLY", "SHOO", "CRT", "TTEK", "HNI", "STX", "JNPR", "FND", "WHR", "BURL",
    "WNC", "HAYW", "NI", "GPC", "LSCC", "BK", "LB", "EPRT", "GXO", "UPLD",
    "ATO", "BRPT", "KRG", "KFY", "ASR", "SIL", "PSTG", "FFIN", "SMPL", "ARII",
    "BSL", "WTRG", "DLB", "CBRL", "GFF", "KDP", "XY", "NUVX", "CLF", "RAMP",
    "AIR", "NSA", "AVT", "LYB", "BIO", "LNC", "MAT", "EQH", "OGN", "RHI",
    # 351-400
    "LCII", "APKS", "WFRD", "BTU", "CLH", "SCCO", "EQT", "WDC", "SEIC", "EWBC",
    "GBCI", "HBNC", "HCCI", "PQ", "PSTV", "BG", "NVEE", "FERG", "TAL", "TRM",
    "CHT", "ATGE", "RIO", "ALK", "SLM", "AR", "TPH", "EV", "LAD", "PLUG",
    "HA", "BDN", "LBRDK", "LBRDA", "MUSA", "DGII", "GDRX", "SMCI", "ION", "ASX",
    "LPLA", "BLCO", "GKOS", "ACA", "LXP", "PLOW", "SEB", "SWC", "AMTM", "LRE",
    # 401-450
    "AMH", "IMAB", "AGX", "PEI", "NGL", "MLKN", "OBE", "SPA", "FRTA", "ENR",
    "THG", "PRKR", "MAG", "MXC", "TCI", "CAPL", "PAM", "AXE", "CTRA", "XFOR",
    "WEX", "PNRA", "REXR", "OPY", "FLR", "MNW", "ARWR", "UFI", "AMF", "AMRX",
    "NEE", "ENZ", "ANDE", "BTE", "PVG", "PAAS", "APLE", "VABK", "AMK", "NGL",
    "BRX", "PFGC", "CGL", "VOYA", "DAR", "SCKT", "PLTK", "TAK", "MGOL", "SCPL",
    
    "CDTX", "SOLN", "SPXC", "ATGE", "VUSE", "CEG", "GDDY", "DBX", "REG", "FELE",
    "NVEC", "LYTS", "PRSP", "OHI", "TPB", "CALM", "TRIN",
    "AGM", "BWXT", "PAC", "IPHI", "BFC", "PRCT", "NZR", "BRNW", "CHD", "RGEN",
    "LW", "DORM", "WLL", "EPD", "FTCH", "CNHI", "ASGN", "MTZ", "OGI",
    "EVA", "BKD", "PWM", "VEEV", "ASLE", "FOSL", "PMVP", "ATNY", "WSM",
]


class MarketDataHandler:
    """Handles market data fetching and technical analysis."""

    def __init__(self):
        """Initialize data handler with Alpaca connector."""
        self.alpaca = AlpacaConnector()
        self.rsi_fast_period = RSI_PERIOD_FAST
        self.rsi_slow_period = RSI_PERIOD_SLOW

    def calculate_rsi(self, prices, period):
        """
        Calculate Relative Strength Index (RSI).

        Args:
            prices: Series of closing prices
            period: RSI period (e.g., 14 or 28)

        Returns:
            Series of RSI values
        """
        delta = prices.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()

        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        return rsi

    def fetch_and_enrich_data(self, symbol):
        """
        Fetch historical data and enrich with RSI indicators.

        Args:
            symbol: Stock ticker

        Returns:
            DataFrame with OHLCV + RSI(14) + RSI(28), or None if fetch fails
        """
        try:
            # Calculate lookback: 6 months + buffer for RSI calculation
            end_date = datetime.now()
            start_date = end_date - timedelta(days=DATA_LOOKBACK_MONTHS * 30 + 100)

            # Fetch bars from yfinance
            bars = yf.download(symbol, start=start_date, end=end_date, progress=False)

            if bars is None or bars.empty:
                return None

            # Handle yfinance column format (may be MultiIndex or tuple)
            if isinstance(bars.columns, pd.MultiIndex):
                # If MultiIndex, get level 0 (column names)
                bars.columns = bars.columns.get_level_values(0)

            # Rename columns to lowercase for consistency
            bars.columns = [str(col).lower() for col in bars.columns]

            # Ensure close column exists
            if 'close' not in bars.columns:
                return None

            # Calculate RSI indicators
            bars['rsi_14'] = self.calculate_rsi(bars['close'], self.rsi_fast_period)
            bars['rsi_28'] = self.calculate_rsi(bars['close'], self.rsi_slow_period)

            # Get the latest row
            latest = bars.iloc[-1]

            return {
                'symbol': symbol,
                'timestamp': bars.index[-1] if len(bars.index) > 0 else datetime.now(),
                'close': float(latest['close']),
                'rsi_14': float(latest['rsi_14']) if pd.notna(latest['rsi_14']) else None,
                'rsi_28': float(latest['rsi_28']) if pd.notna(latest['rsi_28']) else None,
                'bars': bars,  # Return full dataframe for debugging
            }

        except Exception as e:
            print(f"Error enriching data for {symbol}: {e}")
            return None

    def scan_sp500_for_signals(self, skip_symbols=None, on_signal_callback=None):
        """
        Scan S&P 500 for overbought/oversold signals.
        Yields signals immediately as they're found (no wait for full scan).

        Args:
            skip_symbols: List of symbols to skip (e.g., already in position)
            on_signal_callback: Function to call immediately when signal found

        Returns:
            List of all signals found (also yields via callback)
        """
        skip_symbols = skip_symbols or []
        signals = []
        signal_count = 0

        print(f"Scanning {len(SP500_TICKERS)} S&P 500 stocks for RSI confluence...")

        for i, symbol in enumerate(SP500_TICKERS):
            if symbol in skip_symbols:
                print(f"[{i+1}/{len(SP500_TICKERS)}] {symbol}: Skipped (in position)")
                continue

            data = self.fetch_and_enrich_data(symbol)
            time.sleep(1)

            if data is None:
                print(f"[{i+1}/{len(SP500_TICKERS)}] {symbol}: No data")
                continue

            rsi_14 = data['rsi_14']
            rsi_28 = data['rsi_28']

            if rsi_14 is None or rsi_28 is None:
                print(f"[{i+1}/{len(SP500_TICKERS)}] {symbol}: RSI not ready")
                continue

            # Check for RSI confluence
            signal_type = None

            # Oversold: Both RSI < 30
            if rsi_14 < RSI_OVERSOLD_THRESHOLD and rsi_28 < RSI_OVERSOLD_THRESHOLD:
                signal_type = "oversold"

            # Overbought: Both RSI > 70
            elif rsi_14 > RSI_OVERBOUGHT_THRESHOLD and rsi_28 > RSI_OVERBOUGHT_THRESHOLD:
                signal_type = "overbought"

            if signal_type:
                signal_count += 1
                print(f"[{i+1}/{len(SP500_TICKERS)}] {symbol}: ✓ {signal_type.upper()} (RSI14={rsi_14:.2f}, RSI28={rsi_28:.2f})")

                signal = {
                    'symbol': symbol,
                    'signal_type': signal_type,  # "oversold" or "overbought"
                    'rsi_14': rsi_14,
                    'rsi_28': rsi_28,
                    'close': data['close'],
                    'timestamp': data['timestamp'],
                }

                signals.append(signal)

                # Call callback immediately to process entry
                if on_signal_callback:
                    on_signal_callback(signal)
            else:
                print(f"[{i+1}/{len(SP500_TICKERS)}] {symbol}: No signal (RSI14={rsi_14:.2f}, RSI28={rsi_28:.2f})")

        print(f"\n✓ Scan complete. Found {signal_count} signals.\n")
        return signals

    def get_latest_price(self, symbol):
        """Get latest close price for a symbol."""
        data = self.fetch_and_enrich_data(symbol)
        if data:
            return data['close']
        return None
