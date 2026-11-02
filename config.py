"""Static configuration for FX SCAN."""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path
from typing import Final

APP_NAME: Final = "FX SCAN"
VERSION: Final = "1.0.0"

# Watch list: the 7 majors plus the Yen and EUR crosses (18 pairs).
# Edit the groups below - every other module reads PAIRS.
MAJORS: Final[tuple[str, ...]] = (
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "USDCHF",
    "AUDUSD",
    "USDCAD",
    "NZDUSD",
)
YEN_CROSSES: Final[tuple[str, ...]] = (
    "EURJPY",
    "GBPJPY",
    "AUDJPY",
    "NZDJPY",
    "CADJPY",
    "CHFJPY",
)
EUR_CROSSES: Final[tuple[str, ...]] = (
    "EURGBP",
    "EURAUD",
    "EURNZD",
    "EURCAD",
    "EURCHF",
)
PAIRS: Final[tuple[str, ...]] = MAJORS + YEN_CROSSES + EUR_CROSSES
# EURJPY is both a Yen and a EUR cross; it is listed once, under YEN_CROSSES.

# --- Data ---------------------------------------------------------------
HISTORY_PERIOD: Final = "3mo"  # how much history yfinance is asked for
INTRADAY_INTERVAL: Final = "1h"  # daily candles are built from these bars

# A trading "day" runs from SESSION_CUTOFF_HOUR to SESSION_CUTOFF_HOUR in SESSION_TZ.
# 17:00 New York is the FX daily close used by TradingView/OANDA and most brokers.
SESSION_TZ: Final = "America/New_York"
SESSION_CUTOFF_HOUR: Final = 17

# Bump when the meaning of cached candles changes; old caches are wiped on connect.
SCHEMA_VERSION: Final = 2
MIN_BARS: Final = 5  # fewer cached bars than this => cache is unusable
MIN_BARS_FOR_SIGNAL: Final = 2  # latest bar + previous bar

# --- Cache freshness ----------------------------------------------------
# If we already hit the network this recently and still lack the newest
# completed bar (bank holiday, yfinance lag), do not hammer the API again.
RETRY_COOLDOWN: Final = timedelta(minutes=30)
DEFAULT_DB_PATH: Final = Path.home() / ".fx_scan" / "cache.sqlite"


def db_path() -> Path:
    """Cache location; override with the FX_SCAN_DB environment variable."""
    override = os.environ.get("FX_SCAN_DB")
    return Path(override) if override else DEFAULT_DB_PATH


def yahoo_symbol(pair: str) -> str:
    """Map ``EURUSD`` to yfinance's ``EURUSD=X``."""
    return f"{pair.upper()}=X"


def pip_size(pair: str) -> float:
    """Price value of one pip (0.01 for JPY crosses, 0.0001 otherwise)."""
    return 0.01 if pair.upper().endswith("JPY") else 0.0001


def price_decimals(pair: str) -> int:
    """Display precision (pipette) for a pair."""
    return 3 if pair.upper().endswith("JPY") else 5


def display_pair(pair: str) -> str:
    """``EURUSD`` -> ``EUR/USD``."""
    p = pair.upper()
    return f"{p[:3]}/{p[3:]}"
