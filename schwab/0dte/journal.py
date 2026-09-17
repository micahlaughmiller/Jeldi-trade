# ============================================
# TRADE JOURNAL
# Records all trades for analysis
# ============================================

import csv
from pathlib import Path
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

JOURNAL_FILE = Path("astra_journal.csv")

FIELDS = [
    "timestamp",
    "setup_type",
    "direction",
    "equity",
    "tier",
    "spread_width",
    "contracts",
    "spx_entry",
    "overnight_high",
    "overnight_low",
    "or_high",
    "or_low",
    "short_strike",
    "long_strike",
    "credit",
    "stop_price",
    "exit_price",
    "pnl",
    "max_favorable_excursion",
    "max_adverse_excursion",
    "runner",
    "exit_reason",
]


def write_trade(trade):
    """Save trade to CSV journal"""
    
    exists = JOURNAL_FILE.exists()
    
    row = {field: trade.get(field) for field in FIELDS}
    
    if not row["timestamp"]:
        row["timestamp"] = datetime.now().isoformat()
    
    try:
        with JOURNAL_FILE.open("a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            
            if not exists:
                writer.writeheader()
            
            writer.writerow(row)
        
        logger.info(f"✓ Trade journaled: {trade.get('exit_reason')}")
    
    except Exception as e:
        logger.error(f"Failed to write trade: {e}")